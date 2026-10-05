#!/usr/bin/env python3
"""Drive the seven simulated consumers against the platform, concurrently.

    make agents-sim                 # all seven, one pass each
    make agents-sim ROUNDS=5        # five passes, enough traffic for the dashboards
    make agents-sim ONLY=da20,da32

Needs one virtual key per agent. `make agent-keys` mints them from bench/agents.json and
prints them; put them in a file of `agent_id=sk-...` lines and point KEYS at it, or set
SIM_KEY_<agent> in the environment.

READ bench/agents_sim/workloads.py BEFORE REPORTING ANYTHING FROM THIS. These are our own
programs standing in for teams we do not have; they measure what the platform can serve,
not who has adopted it.

WHY CONCURRENT RATHER THAN ONE AFTER ANOTHER

Run in sequence, seven workloads are seven separate single-tenant measurements. Run
together they are one multi-tenant measurement, which is the only way the thing being
claimed -- separate quota, separate attribution, no interference between projects -- can
actually fail in front of you.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.agents_sim.workloads import WORKLOADS, Workload  # noqa: E402


class Result:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.ok = 0
        self.refused: collections.Counter[str] = collections.Counter()
        self.errors: collections.Counter[str] = collections.Counter()
        self.latencies: list[float] = []
        # Adversarial inputs that were NOT refused. Any value above zero is the headline.
        self.missed_attacks = 0
        self.attacks_sent = 0


def _call(base: str, key: str, agent_id: str, model: str,
          messages: list[dict], max_tokens: int, timeout: float) -> tuple[int, dict]:
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Without this every project lands in the same Prometheus series and the
            # per-project dashboards -- the thing this run exists to populate -- stay empty.
            "X-Agent-Id": agent_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            return exc.code, {}
        finally:
            exc.close()


def _messages(w: Workload, text: str) -> list[dict]:
    msgs = []
    if w.system:
        msgs.append({"role": "system", "content": w.system})
    msgs.append({"role": "user", "content": text})
    return msgs


def run_one(w: Workload, base: str, key: str, rounds: int, timeout: float) -> Result:
    res = Result(w.agent_id)
    for _ in range(rounds):
        for text in w.inputs:
            started = time.monotonic()
            status, body = _call(base, key, w.agent_id, w.model,
                                 _messages(w, text), w.max_tokens, timeout)
            res.latencies.append(time.monotonic() - started)
            if status == 200:
                res.ok += 1
            elif status == 400:
                res.refused[(body.get("error") or {}).get("code") or "unknown"] += 1
            else:
                res.errors[str(status)] += 1

        # The guardrail has to be exercised, not assumed. A run of clean traffic cannot
        # tell a working injection filter from one that was switched off.
        for text, expected in w.adversarial:
            res.attacks_sent += 1
            status, body = _call(base, key, w.agent_id, w.model,
                                 _messages(w, text), w.max_tokens, timeout)
            code = (body.get("error") or {}).get("code")
            if status == 400 and code == expected:
                res.refused[code] += 1
            else:
                res.missed_attacks += 1
    return res


def _keys(path: str | None) -> dict[str, str]:
    keys: dict[str, str] = {}
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip()
    for w in WORKLOADS:
        env = os.getenv(f"SIM_KEY_{w.agent_id}")
        if env:
            keys[w.agent_id] = env
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=os.getenv("MOC_BASE_URL", ""))
    ap.add_argument("--keys", default=os.getenv("KEYS", ""))
    ap.add_argument("--rounds", type=int, default=int(os.getenv("ROUNDS", "1")))
    ap.add_argument("--only", default=os.getenv("ONLY", ""))
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()

    if not a.base_url:
        print("can --base-url (hoac MOC_BASE_URL), vi du https://.../v1")
        return 2

    chosen = [w for w in WORKLOADS
              if not a.only or w.agent_id in {s.strip() for s in a.only.split(",")}]
    keys = _keys(a.keys or None)
    missing = [w.agent_id for w in chosen if w.agent_id not in keys]
    if missing:
        print(f"thieu key cho: {', '.join(missing)}")
        print("Chay 'make agent-keys', roi dat vao tep dang 'da19=sk-...' va tro KEYS toi no.")
        return 2

    print(f"\n  {len(chosen)} workload mo phong, {a.rounds} vong, dong thoi")
    print("  LUU Y: day la chuong trinh cua chinh nhom, khong phai bay de an that.\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        futures = {pool.submit(run_one, w, a.base_url, keys[w.agent_id],
                               a.rounds, a.timeout): w for w in chosen}
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    results.sort(key=lambda r: r.agent_id)
    print(f"  {'agent':8} {'model':22} {'ok':>4} {'tu choi':>8} {'loi':>5} {'p95':>8}")
    print("  " + "-" * 62)
    total_missed = 0
    for r in results:
        w = next(x for x in WORKLOADS if x.agent_id == r.agent_id)
        lat = sorted(r.latencies)
        p95 = lat[int(len(lat) * 0.95)] if lat else 0.0
        total_missed += r.missed_attacks
        print(f"  {r.agent_id:8} {w.model:22} {r.ok:>4} {sum(r.refused.values()):>8} "
              f"{sum(r.errors.values()):>5} {p95:>7.2f}s")
        for stage, n in r.refused.most_common():
            print(f"           tu choi tai {stage}: {n}")
        for code, n in r.errors.most_common():
            print(f"           HTTP {code}: {n}")

    print()
    attacks = sum(r.attacks_sent for r in results)
    if total_missed:
        print(f"  CANH BAO: {total_missed}/{attacks} mau tan cong KHONG bi chan.")
        return 1
    if attacks:
        print(f"  {attacks}/{attacks} mau tan cong bi chan dung tang.")
    print("  Dashboard theo agent se co du lieu duoi bay end_user khac nhau.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
