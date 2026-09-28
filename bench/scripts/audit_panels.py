#!/usr/bin/env python3
"""Find dashboard panels, recording rules and alerts that can never show data.

WHY A PANEL THAT IS ALWAYS EMPTY IS WORSE THAN NO PANEL

An empty panel does not read as "this metric does not exist". It reads as an outage. It
sends whoever is on call looking for a failure that was never there, and -- much worse --
it teaches them that empty panels on this dashboard are normal. After that, the one panel
that goes empty because something really broke is the one nobody looks at twice.

This repo already paid for that lesson twice in one session: an Aurora panel that read
"No data" and turned out to be fine, and a vLLM request-rate panel that read zero while
the platform served every request from cache. Both times the dashboard was telling the
truth and the truth was unreadable.

THE DISTINCTION THIS SCRIPT EXISTS TO MAKE

Empty has two causes and they need opposite responses:

    no traffic yet          -> send traffic, the panel is fine
    metric cannot exist     -> delete the panel, it will never work

So run this WITH LOAD RUNNING. `make load-incluster SCENARIO=ramp` in one terminal and
this in another: anything still empty while the platform is saturated is empty by
construction, not by circumstance.

AND IT MUST NOT CONFUSE "FAILED" WITH "EMPTY"

The first version of this check had no credentials for the Prometheus ingress, treated
every 401 as an absent metric, and reported that 57 of 57 metrics were missing -- which
would have deleted the entire observability stack. Every query failure is therefore fatal
here and stops the run, because a checker that cannot reach its data has no findings to
report, only noise.

    make dashboard-audit
    make dashboard-audit PROM=http://127.0.0.1:9090      # qua port-forward, khong can auth
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "observability" / "dashboards"
RULES = ROOT / "observability" / "rules"

# PromQL keywords and common label names, none of which are metric names.
NOT_A_METRIC = {
    "sum", "rate", "avg", "max", "min", "by", "le", "on", "group_left", "group_right",
    "without", "increase", "histogram_quantile", "label_replace", "topk", "bottomk",
    "count", "irate", "clamp_max", "clamp_min", "vector", "time", "or", "and", "unless",
    "offset", "ignoring", "quantile", "stddev", "absent", "delta", "idelta", "changes",
    "round", "ceil", "floor", "sort_desc", "sort", "abs", "predict_linear", "deriv",
    "last_over_time", "avg_over_time", "max_over_time", "min_over_time", "sum_over_time",
    "count_over_time", "present_over_time", "namespace", "pod", "container", "instance",
    "job", "model", "cluster", "node", "true", "false", "scalar", "default", "type",
    "interval", "range", "step", "quantile_over_time", "stddev_over_time",
}
# Metric families this platform defines. Anything outside them is a label or a keyword.
PREFIXES = ("vllm", "litellm", "guardrail", "DCGM", "platform", "agent", "kube",
            "node_", "container_", "up")
TOKEN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*:[a-zA-Z0-9_:]+|[a-zA-Z_][a-zA-Z0-9_]{3,})\b")


def metric_names(expr: str) -> set[str]:
    found = set()
    for name in TOKEN.findall(expr):
        if name in NOT_A_METRIC or name.isdigit():
            continue
        if ":" in name or name.startswith(PREFIXES):
            found.add(name)
    return found


def query(prom: str, auth: str, expr: str) -> tuple[str, object]:
    url = prom.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    request = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(auth.encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except Exception as exc:  # noqa: BLE001 -- every failure is fatal, see the docstring
        return "ERROR", f"{type(exc).__name__}: {exc}"[:90]
    if body.get("status") != "success":
        return "ERROR", str(body.get("error"))[:90]
    return "OK", len(body["data"]["result"])


def walk(node: dict):
    for panel in node.get("panels", []):
        yield panel
        yield from walk(panel)


def collect() -> dict[str, set[str]]:
    """metric name -> the places that reference it."""
    where: dict[str, set[str]] = {}
    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        for panel in walk(board):
            for target in panel.get("targets") or []:
                for name in metric_names(target.get("expr", "")):
                    where.setdefault(name, set()).add(
                        f"{path.name} :: {panel.get('title', '(khong ten)')}")
    recording = (RULES / "recording.yaml")
    if recording.exists():
        for name in re.findall(r"- record:\s*(\S+)", recording.read_text(encoding="utf-8")):
            where.setdefault(name, set()).add("recording.yaml :: output")
    return where


def main() -> int:
    prom = os.environ.get("PROM", "http://127.0.0.1:9090")
    auth = os.environ.get("PROM_AUTH", "")
    where = collect()
    print(f"  {len(where)} metric duy nhat, kiem tra tren {prom}")

    results: dict[str, tuple[str, object]] = {}
    for name in sorted(where):
        results[name] = query(prom, auth, name)

    errors = [n for n, (state, _) in results.items() if state == "ERROR"]
    if errors:
        print(f"\n  {len(errors)} QUERY LOI -- khong ket luan duoc gi.", file=sys.stderr)
        for name in errors[:3]:
            print(f"    {name}: {results[name][1]}", file=sys.stderr)
        print("\n  Prometheus sau ingress can basic auth:", file=sys.stderr)
        print("    PROM_AUTH=\"admin:<mat khau>\" make dashboard-audit", file=sys.stderr)
        print("  hoac chay qua port-forward: make pf && make dashboard-audit", file=sys.stderr)
        return 2

    empty = sorted(n for n, (_, count) in results.items() if count == 0)
    live = len(results) - len(empty)
    print(f"  {live} co du lieu, {len(empty)} rong\n")
    if not empty:
        print("  Khong co panel nao rong. (Nho: chay cung luc voi tai, neu khong thi")
        print("  'rong' chi co nghia la chua ai gui request.)")
        return 0

    print(f"  RONG ({len(empty)}) -- chay cung luc voi tai thi day la rong VINH VIEN:")
    for name in empty:
        print(f"    {name}")
        for place in sorted(where[name]):
            print(f"        {place}")
    print()
    print("  Rong vinh vien thi xoa, dung de lai. Mot panel luon trong se day nguoi truc")
    print("  di tim su co khong co that, va lam quen mat rang panel trong la bat thuong.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
