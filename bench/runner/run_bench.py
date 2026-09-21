#!/usr/bin/env python3
"""Run one measurement and make the result traceable.

This does NOT implement a load generator. `vllm bench serve` already does that, and it
already computes the metric this project is built around -- request goodput, the fraction
of requests meeting a TTFT and a TPOT objective at the same time -- along with ITL
percentiles, Poisson arrivals, warm-up, and per-request detail. Re-implementing that in a
few hundred lines of asyncio would produce numbers that are not comparable to anything
published, and would put the subtle bugs in the client rather than in the engine.

What is missing around it, and what this adds:

  1. A quiet start. A run that begins while the previous run's sequences are still
     draining measures the tail of that run. The engine is polled through Prometheus
     until the KV-cache is empty and nothing is in flight.
  2. Lineage. A JSON number is worthless in week five unless it carries the engine
     arguments, image tags, dataset version and checksum, and git commit that produced
     it.
  3. Durable output. Results go to S3 immediately, because the cluster is destroyed at
     the end of every session and anything inside it goes with it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[run_bench] {msg}", flush=True)


def prom_query(prom_url: str, query: str, timeout: int = 10) -> float | None:
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except Exception as exc:  # network, DNS, 5xx -- all mean "cannot tell"
        log(f"prometheus query failed ({exc}); treating as unknown")
        return None
    result = data.get("data", {}).get("result", [])
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


def wait_until_idle(prom_url: str, timeout_s: int, kv_max: float = 0.05) -> bool:
    """Block until the engine has drained, or give up and say so.

    Starting a run on a warm engine inflates nothing and deflates nothing predictably --
    it just makes the run incomparable to the others, which is worse than either.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        kv = prom_query(prom_url, "vllm:gpu_cache_usage_perc")
        running = prom_query(prom_url, "vllm:num_requests_running")
        waiting = prom_query(prom_url, "vllm:num_requests_waiting")
        if kv is None and running is None:
            log("cannot read engine state from Prometheus; continuing without the idle gate")
            return False
        if (kv or 0) < kv_max and (running or 0) < 1 and (waiting or 0) < 1:
            log(f"engine idle (kv={kv}, running={running}, waiting={waiting})")
            return True
        log(f"waiting for idle: kv={kv}, running={running}, waiting={waiting}")
        time.sleep(5)
    log(f"engine did not go idle within {timeout_s}s; running anyway and recording the fact")
    return False


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        # In the runner image there is no checkout; the image build stamps it instead.
        return os.getenv("GIT_COMMIT", "unknown")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- main
def build_vllm_cmd(a, out_dir: Path, result_name: str) -> list[str]:
    cmd = [
        "vllm", "bench", "serve",
        "--backend", "openai-chat",
        "--endpoint", "/v1/chat/completions",
        "--base-url", a.url,
        "--model", a.model,
        "--served-model-name", a.model,
        "--dataset-name", "custom",
        "--dataset-path", a.dataset,
        "--num-prompts", str(a.num_prompts),
        # Both objectives at once. A request that returns fast but stutters is not a
        # success, and neither is a smooth one that took three seconds to start.
        "--goodput", f"ttft:{int(a.ttft_slo_ms)}", f"tpot:{int(a.tpot_slo_ms)}",
        "--percentile-metrics", "ttft,tpot,itl,e2el",
        "--metric-percentiles", "50,95,99",
        "--num-warmups", str(a.warmups),
        # Fixed output length per sample. Without it the model stops at its own EOS,
        # output length varies with the prompt, and two runs of the "same" configuration
        # are no longer measuring the same work.
        "--ignore-eos",
        "--save-result", "--save-detailed",
        "--result-dir", str(out_dir),
        "--result-filename", result_name,
        "--disable-tqdm",
    ]
    if a.mode == "open":
        # Poisson arrivals: the generator keeps offering work no matter how slow the
        # replies get. A closed loop throttles itself and hides saturation.
        cmd += ["--request-rate", str(a.rate), "--burstiness", "1.0"]
    else:
        cmd += ["--request-rate", "inf", "--max-concurrency", str(a.concurrency)]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("VLLM_URL", "http://vllm-a.inference:8000"))
    ap.add_argument("--prom", default=os.getenv("PROM_URL",
                    "http://kps-kube-prometheus-stack-prometheus.monitoring:9090"))
    ap.add_argument("--model", default=os.getenv("MODEL", "qwen2.5-7b"))
    ap.add_argument("--dataset", default=os.getenv("DATASET", "/data/chat-v1.jsonl"))
    ap.add_argument("--mode", choices=["open", "closed"], default="open",
                    help="open = Poisson arrivals (the headline result); "
                         "closed = fixed concurrency (finds the technical ceiling)")
    ap.add_argument("--rate", type=float, default=2.0, help="requests/s, open mode")
    ap.add_argument("--concurrency", type=int, default=16, help="closed mode")
    ap.add_argument("--num-prompts", type=int, default=500)
    ap.add_argument("--warmups", type=int, default=20)
    ap.add_argument("--ttft-slo-ms", type=float, default=500)
    ap.add_argument("--tpot-slo-ms", type=float, default=40)
    ap.add_argument("--idle-timeout", type=int, default=300)
    ap.add_argument("--out-dir", default=os.getenv("OUT_DIR", "/results"))
    ap.add_argument("--s3-prefix", default=os.getenv("S3_PREFIX", ""))
    ap.add_argument("--tag", default=os.getenv("TAG", ""))
    a = ap.parse_args()

    if shutil.which("vllm") is None:
        log("the `vllm` CLI is not on PATH -- this is meant to run in the runner image, "
            "which is built FROM vllm/vllm-openai")
        return 2

    dataset = Path(a.dataset)
    if not dataset.exists():
        log(f"dataset not found: {dataset}")
        return 2

    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_name = f"{run_id}.vllm.json"

    log(f"run_id={run_id} mode={a.mode} dataset={dataset.name}")
    idle = wait_until_idle(a.prom, a.idle_timeout)

    cmd = build_vllm_cmd(a, out_dir, raw_name)
    log("exec: " + " ".join(cmd))
    t0 = time.time()
    proc = subprocess.run(cmd, text=True)
    wall = time.time() - t0

    raw_path = out_dir / raw_name
    raw = json.loads(raw_path.read_text()) if raw_path.exists() else None
    if proc.returncode != 0 or raw is None:
        log(f"vllm bench serve exited {proc.returncode}; recording the failure")

    doc = {
        "run_id": run_id,
        "lineage": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_seconds": round(wall, 2),
            "tag": a.tag,
            "mode": a.mode,
            "rate": a.rate if a.mode == "open" else None,
            "concurrency": a.concurrency if a.mode == "closed" else None,
            "num_prompts": a.num_prompts,
            "warmups": a.warmups,
            "ttft_slo_ms": a.ttft_slo_ms,
            "tpot_slo_ms": a.tpot_slo_ms,
            "model": a.model,
            "dataset": dataset.name,
            # The checksum is what makes two runs comparable. Without it, "same dataset"
            # is a claim rather than a fact.
            "dataset_sha256": sha256_of(dataset),
            # Supplied by the Job manifest; they are the engine configuration under test.
            "engine_args": os.getenv("ENGINE_ARGS", "unknown"),
            "vllm_image": os.getenv("VLLM_IMAGE", "unknown"),
            "runner_image": os.getenv("RUNNER_IMAGE", "unknown"),
            "git_commit": git_commit(),
            # Recorded, not hidden: a run that started on a busy engine is still a data
            # point, but it must be possible to exclude it later.
            "started_from_idle": idle,
            "benchmark_tool": "vllm bench serve",
            "exit_code": proc.returncode,
        },
        "vllm_result": raw,
    }

    doc_path = out_dir / f"{run_id}.json"
    doc_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")

    if raw:
        g = raw.get("request_goodput")
        log(f"goodput={g} req/s  throughput={raw.get('request_throughput')} req/s  "
            f"output={raw.get('output_throughput')} tok/s")
        log(f"ttft p99={raw.get('p99_ttft_ms')}ms  tpot p99={raw.get('p99_tpot_ms')}ms  "
            f"itl p99={raw.get('p99_itl_ms')}ms")

    if a.s3_prefix:
        try:
            import boto3
            bucket, _, key_prefix = a.s3_prefix.removeprefix("s3://").partition("/")
            s3 = boto3.client("s3")
            for f in (doc_path, raw_path):
                if f.exists():
                    key = f"{key_prefix.rstrip('/')}/{f.name}"
                    s3.upload_file(str(f), bucket, key)
                    log(f"uploaded s3://{bucket}/{key}")
        except Exception as exc:
            log(f"S3 upload FAILED ({exc}) -- the result exists only inside this pod, "
                f"which is destroyed at session end. Copy it out before then.")
            return 1

    log(f"run_id={run_id}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
