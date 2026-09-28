#!/usr/bin/env python3
"""Ship the guardrail's audit log to S3, so it outlives the cluster that produced it.

WHY THIS IS NOT OPTIONAL PLUMBING

The cluster is destroyed every evening. Everything inside it goes with it, including
every record of what the platform was asked and what it answered. Prometheus already has
`make snapshot` for exactly this reason; the audit log had nothing, so the one artefact
that can answer "which prompt, which model, what came back" existed only until teardown.

That makes a whole class of question unanswerable the morning after: why a benchmark run
scored what it did, whether a refusal was correct, what the model actually said when a
reviewer asks. Metrics keep the shape of the day and lose the content.

WHAT GOES TO S3, AND WHAT DELIBERATELY DOES NOT

The records are what the guardrail already writes with AUDIT_LOG on, and they carry the
REDACTED question -- `[CCCD_1]` where an identity number was -- plus the kinds that were
replaced. The raw value never enters the log and therefore never reaches S3. See the
_audit docstring in services/llm_pipeline/app.py.

They are still content, so the destination is the artifacts bucket the project already
owns, under a prefix that says what they are. Treat the prefix as sensitive even though
it is redacted: a question can identify a person without containing an identity number.

USAGE
    make audit-export                    # this session, to s3://<artifacts>/audit/
    make audit-export HOURS=4 LABEL=ramp
    make audit-export LOCAL_ONLY=1       # just write results/audit/, skip S3
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"


def collect(namespace: str, hours: int) -> list[dict]:
    """Read audit lines out of the guardrail's stdout.

    Not a stream: this runs at the end of a session, when the questions of interest have
    already been asked. A streaming shipper would be better and is more moving parts than
    a nightly lab needs.
    """
    code, out, err = run([
        "kubectl", "-n", namespace, "logs", "deploy/guardrail",
        "-c", "guardrail", f"--since={hours}h",
    ], timeout=300)
    if code != 0:
        print(f"  khong doc duoc log guardrail: {err.strip()[:160]}", file=sys.stderr)
        return []
    records = []
    for line in out.splitlines():
        line = line.strip()
        # The guardrail's stdout also carries startup messages and tracebacks. Match on
        # the marker rather than on "looks like JSON".
        if '"kind": "audit"' not in line:
            continue
        start = line.find('{"kind": "audit"')
        if start < 0:
            continue
        try:
            records.append(json.loads(line[start:]))
        except ValueError:
            continue
    return records


def summarise(records: list[dict]) -> dict:
    """A small index beside the raw file, so a reader can tell what is in it."""
    outcomes: dict[str, int] = {}
    stages: dict[str, int] = {}
    models: dict[str, int] = {}
    agents: dict[str, int] = {}
    pii_kinds: dict[str, int] = {}
    retried = 0
    cached = 0
    for r in records:
        outcomes[r.get("outcome", "?")] = outcomes.get(r.get("outcome", "?"), 0) + 1
        if r.get("outcome") != "allowed":
            stages[r.get("stage", "?")] = stages.get(r.get("stage", "?"), 0) + 1
        models[r.get("model", "?")] = models.get(r.get("model", "?"), 0) + 1
        agents[r.get("agent", "?")] = agents.get(r.get("agent", "?"), 0) + 1
        for finding in r.get("pii_inbound") or []:
            k = finding.get("kind", "?")
            pii_kinds[k] = pii_kinds.get(k, 0) + 1
        retried += 1 if r.get("retried") else 0
        cached += 1 if r.get("cache_hit") else 0
    times = [r["ts"] for r in records if isinstance(r.get("ts"), (int, float))]
    return {
        "records": len(records),
        "from": datetime.fromtimestamp(min(times), timezone.utc).isoformat() if times else None,
        "to": datetime.fromtimestamp(max(times), timezone.utc).isoformat() if times else None,
        "outcomes": outcomes,
        "refusal_stages": stages,
        "models": models,
        "agents": agents,
        "pii_inbound_kinds": pii_kinds,
        "retried_after_empty": retried,
        "cache_hits": cached,
        "note": "questions are REDACTED; pii_inbound lists the kinds replaced, never values",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--namespace", default="llm-serving")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--label", default="")
    ap.add_argument("--bucket", default="")
    ap.add_argument("--local-only", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{args.label}" if args.label else stamp

    records = collect(args.namespace, args.hours)
    if not records:
        # Not an error worth failing a teardown over: AUDIT_LOG is off by default, so an
        # empty result usually means nobody turned it on, not that anything broke.
        print("  khong co ban ghi audit nao (AUDIT_LOG co bat khong? 'make audit-on')")
        return 0

    out_dir = ROOT / "results" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"{name}.jsonl.gz"
    with gzip.open(jsonl, "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    index = summarise(records)
    (out_dir / f"{name}.summary.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    size = jsonl.stat().st_size
    print(f"  {len(records)} ban ghi -> {jsonl.relative_to(ROOT)} ({size/1024:.0f} KB)")
    print(f"  ket qua: {index['outcomes']}")
    if index["refusal_stages"]:
        print(f"  tu choi theo stage: {index['refusal_stages']}")
    if index["pii_inbound_kinds"]:
        print(f"  PII da che: {index['pii_inbound_kinds']}")
    if index["retried_after_empty"]:
        print(f"  thu lai sau khi sinh rong: {index['retried_after_empty']}")

    if args.local_only:
        return 0

    bucket = args.bucket
    if not bucket:
        code, out, _ = run(["terraform", "-chdir=terraform/core",
                            "output", "-raw", "artifacts_bucket_name"], timeout=90)
        bucket = out.strip() if code == 0 else ""
    if not bucket or "/" in bucket or " " in bucket:
        print("  khong lay duoc ten bucket; file van nam o results/audit/", file=sys.stderr)
        return 0
    if not shutil.which("aws"):
        print("  khong co aws CLI; file van nam o results/audit/", file=sys.stderr)
        return 0

    for path in (jsonl, out_dir / f"{name}.summary.json"):
        dest = f"s3://{bucket}/audit/{name}/{path.name}"
        code, _, err = run(["aws", "s3", "cp", str(path), dest], timeout=300)
        if code != 0:
            print(f"  upload that bai: {err.strip()[:140]}", file=sys.stderr)
            return 1
        print(f"  -> {dest}")
    print()
    print("  Doc lai sau khi cum da bi xoa:")
    print(f"    aws s3 ls s3://{bucket}/audit/")
    print(f"    aws s3 cp s3://{bucket}/audit/{name}/{jsonl.name} - | gunzip | head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
