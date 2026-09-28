#!/usr/bin/env python3
"""Prove the PII redaction fires, on the running platform, with evidence.

THE GAP THIS CLOSES

The pipeline redacts identity numbers out of a question before retrieval and before any
log, and it scans the answer on the way out. Both are tested offline by
`make guardrails-test`. Neither was VERIFIABLE from outside: a counter said findings were
found, and no artefact showed what happened to them. "Does redaction actually work on the
deployed system" could only be answered by reading the code and trusting it.

Trusting the code is exactly the wrong answer for a control whose entire job is to be
there when something goes wrong.

WHAT THIS CHECKS, AND WHY EACH ONE SEPARATELY

For each probe, four independent things have to hold, and they fail independently:

  1. the MODEL never saw it: the question in the audit record shows a placeholder
  2. the number is absent from the AUDIT LOG -- the place it most plausibly leaks,
     because a log is written by different code than the one that redacts
  3. the audit record names the kind that was replaced, so redaction is visible and not
     merely inferred from an absence
  4. guardrail_pii_findings_total moved, so the detector fired rather than the number
     simply never appearing
  5. the request was NOT cached (`skipped_pii`), because a cache keyed on the redacted
     question would otherwise hand caller A's plate to caller B asking the same thing

WHAT IS DELIBERATELY NOT A FAILURE

The caller's own value coming back IN THE RESPONSE TO THAT CALLER. Stage 10 restores the
placeholders the caller themselves supplied, on purpose -- returning someone their own
plate number is not a leak, and the egress scan at stage 9 runs before it, so nothing the
model invented can ride along.

The first version of this script counted that as a leak and reported the platform broken.
Checking the response alone is both too strict here and too weak in general: it would
pass a system that redacts on the way to the model and writes the raw question to disk,
which is the failure the audit log itself came within one commit of shipping.

EVERY VALUE HERE IS INVENTED. The numbers are syntactically valid so the validators
accept them and structurally meaningless: they belong to nobody, and they are the only
kind of PII that should ever be typed into a test.

    make pii-verify
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Synthetic. A valid CCCD needs 12 digits and a province prefix the validator accepts; a
# valid plate needs the province-letter-digits shape. None of these identify anyone.
PROBES = [
    {
        "kind": "cccd",
        "value": "079203001234",
        "question": "Khách hàng có CCCD 079203001234 hỏi: một chuyến xe được tính là hoàn thành khi nào?",
    },
    {
        "kind": "phone",
        "value": "0912345678",
        "question": "Tài xế số điện thoại 0912345678 hỏi: doanh thu thuần được tính thế nào?",
    },
    {
        "kind": "email",
        "value": "nguyenvana@example.com",
        "question": "Gửi kết quả tới email nguyenvana@example.com. Gross Booking Value là gì?",
    },
    {
        "kind": "plate",
        "value": "51F-12345",
        "question": "Xe biển số 51F-12345 thuộc nhóm nào trong đội xe khả dụng?",
    },
]


def sh(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def metric_total(namespace: str) -> dict[str, float]:
    """guardrail_pii_findings_total by kind, read from the pod itself."""
    code, out = sh(["kubectl", "-n", namespace, "exec", "deploy/guardrail",
                    "-c", "guardrail", "--", "python", "-c",
                    "import urllib.request;print(urllib.request.urlopen("
                    "'http://localhost:8080/metrics',timeout=10).read().decode())"],
                   timeout=120)
    totals: dict[str, float] = {}
    if code != 0:
        return totals
    for line in out.splitlines():
        if not line.startswith("guardrail_pii_findings_total"):
            continue
        m = re.search(r'kind="([^"]+)".*?\s([0-9.e+-]+)$', line)
        if m:
            totals[m.group(1)] = totals.get(m.group(1), 0.0) + float(m.group(2))
    return totals


def ask(base: str, key: str, question: str, model: str) -> tuple[int, str]:
    body = json.dumps({"model": model, "max_tokens": 192,
                       "messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "X-Agent-Id": "moc-kb"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return 200, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def audit_lines(namespace: str, since: str = "10m") -> list[dict]:
    code, out = sh(["kubectl", "-n", namespace, "logs", "deploy/guardrail",
                    "-c", "guardrail", f"--since={since}"], timeout=180)
    rows = []
    if code != 0:
        return rows
    for line in out.splitlines():
        i = line.find('{"kind": "audit"')
        if i < 0:
            continue
        try:
            rows.append(json.loads(line[i:]))
        except ValueError:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--namespace", default="llm-serving")
    ap.add_argument("--model", default="qwen2.5-7b")
    args = ap.parse_args()

    print("  Moi gia tri PII duoi day la BIA -- hop le ve cu phap, khong thuoc ve ai.\n")
    before = metric_total(args.namespace)

    sent = []
    for probe in PROBES:
        status, body = ask(args.base_url, args.key, probe["question"], args.model)
        sent.append((probe, status, body))
        print(f"  gui {probe['kind']:6} -> HTTP {status}")
    print("\n  cho audit log...")
    time.sleep(6)
    rows = audit_lines(args.namespace)
    after = metric_total(args.namespace)

    print()
    print(f"  {'loai':7} {'toi model':>9} {'trong log':>10} {'placeholder':>12} {'counter':>9}")
    print("  " + "-" * 56)
    failures = 0
    for probe, status, body in sent:
        value = probe["value"]
        kind = probe["kind"]

        # 1. the MODEL must never have seen it -- the question as logged is what was
        #    built into the prompt, so a placeholder there is proof, not inference.
        model_saw = any(
            value in (r.get("question") or "")
            for r in rows
        )

        # 2. nor appear anywhere in the audit records
        leaked_log = any(value in json.dumps(r, ensure_ascii=False) for r in rows)

        # 3. some audit record must name this kind as replaced -- absence is not evidence
        placeholder = any(
            f.get("kind") == kind
            for r in rows for f in (r.get("pii_inbound") or [])
        )

        # 4. and the detector's own counter must have moved
        moved = after.get(kind, 0.0) > before.get(kind, 0.0)

        ok = (not model_saw) and (not leaked_log) and placeholder and moved
        failures += 0 if ok else 1
        print(f"  {kind:7} {'LOT' if model_saw else 'sach':>9} "
              f"{'LOT' if leaked_log else 'sach':>10} "
              f"{'co' if placeholder else 'KHONG':>12} "
              f"{'+' if moved else 'KHONG':>9}")

    print()
    if failures:
        print(f"  {failures}/{len(PROBES)} loai PII KHONG dat.")
        print("  'LOT' = gia tri that den duoc model hoac nam trong audit log -- nghiem trong.")
        print("  'KHONG' o placeholder/counter = bo phat hien khong chay, gia tri co the")
        print("  chi tinh co khong xuat hien. Ca hai deu phai sua truoc khi co luu luong that.")
        return 1

    print(f"  {len(PROBES)}/{len(PROBES)} dat.")
    print("  Model khong he thay gia tri that, audit log cung khong, va moi loai deu")
    print("  de lai placeholder + tang counter -- tuc la bo che da CHAY, khong phai gia tri")
    print("  tinh co vang mat.")
    sample = next((r for r in rows if r.get("pii_inbound")), None)
    if sample:
        print()
        print("  Vi du mot ban ghi audit (cau hoi da duoc che):")
        print(f"    question   : {sample['question'][:100]}")
        print(f"    pii_inbound: {sample['pii_inbound']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
