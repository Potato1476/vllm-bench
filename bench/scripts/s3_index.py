#!/usr/bin/env python3
"""Write a human-readable index at the top of the artifacts bucket.

WHY A BUCKET OF TIMESTAMPS IS NOT SHARED EVIDENCE

Everything this project measures ends up in S3, because the cluster is destroyed every
evening and anything left inside it goes with it. That part works. What it produces is a
list of prefixes named `20260930T050245Z-day4-4gpu/`, each holding one gzipped file, and
nothing that says which run answered which question.

A teammate opening that sees eight timestamps. To find out whether the 40 req/s figure
came from the third or the sixth, they have to download and unpack several of them. That
is not evidence anyone can check; it is evidence someone could reconstruct if they already
knew what they were looking for.

So this reads the summary each export already writes, adds what the run was for, and puts
one INDEX.md at the root. It is regenerated, never hand-edited -- an index that drifts
from the bucket is worse than none, because it is believed.

    make s3-index
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What each run was for. Keyed by the label an export was given, because the timestamp
# alone cannot carry intent and the person who ran it is the only one who knows.
KNOWN = {
    "w1-ramp": "Tuần 1: ramp đầu tiên, FP16, 1 GPU — trước mọi tối ưu",
    "day3": "Ngày 3: 1 GPU, sau khi rút ngắn câu trả lời (dung lượng 1 → 10 req/s)",
    "day3-steady": "Ngày 3: chạy bền 5 req/s, chốt TC1b lần đầu",
    "ramp30": "Ngày 3: ramp với quy tắc 30 từ",
    "day4-4gpu": "Ngày 4: 4× L4, ramp 10→60 req/s + chạy bền 40 req/s (TC1a, TC1b)",
}


def sh(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def bucket_name() -> str:
    code, out = sh(["terraform", "-chdir=terraform/core", "output", "-raw",
                    "artifacts_bucket_name"], timeout=90)
    name = out.strip()
    if code != 0 or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name):
        return ""
    return name


def listing(bucket: str, prefix: str) -> list[tuple[str, int]]:
    code, out = sh(["aws", "s3", "ls", f"s3://{bucket}/{prefix}", "--recursive"])
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            rows.append((parts[3], int(parts[2])))
    return rows


def describe(key: str) -> str:
    for label, text in KNOWN.items():
        if label in key:
            return text
    return ""


def main() -> int:
    bucket = bucket_name()
    if not bucket:
        print("khong lay duoc ten bucket -- tang core con song khong?", file=sys.stderr)
        return 1

    audit = listing(bucket, "audit/")
    runs = listing(bucket, "runs/")
    models = listing(bucket, "models/")
    datasets = listing(bucket, "datasets/")

    def table(rows: list[tuple[str, int]], skip_summary: bool = True) -> list[str]:
        out = []
        groups: dict[str, list[tuple[str, int]]] = {}
        for key, size in rows:
            top = key.split("/")[1] if key.count("/") >= 1 else key
            groups.setdefault(top, []).append((key, size))
        for top in sorted(groups, reverse=True):
            total = sum(s for _, s in groups[top])
            note = describe(top)
            out.append(f"| `{top}` | {total/1024:,.0f} KB | {note} |")
        return out

    lines = [
        "# Kho bằng chứng — Đề tài 51",
        "",
        f"Tự sinh bởi `bench/scripts/s3_index.py` · "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**Đừng sửa tay file này.** Nó được sinh lại mỗi lần chạy `make s3-index`;",
        "một index lệch với bucket còn tệ hơn không có, vì người ta sẽ tin nó.",
        "",
        "Báo cáo và kế hoạch nằm trong git, không nằm ở đây:",
        "`docs/acceptance-report.md` và `docs/completion-plan.md`.",
        "",
        "---",
        "",
        "## `audit/` — từng request: prompt, model, tài liệu, trích dẫn, kết quả",
        "",
        "Câu hỏi đã **che PII** (`[CCCD_1]` thay cho giá trị thật) và `pii_inbound` ghi",
        "loại đã thay. Giá trị thật không bao giờ được ghi.",
        "",
        "| phiên | dung lượng | nội dung |",
        "|---|---|---|",
    ]
    lines += table(audit)
    lines += [
        "",
        "```bash",
        f"aws s3 cp s3://{bucket}/audit/<phien>/<file>.jsonl.gz - | gunzip | jq .",
        f"aws s3 cp s3://{bucket}/audit/<phien>/<file>.summary.json -   # chỉ mục, không cần giải nén",
        "```",
        "",
        "## `runs/` — probe từng request và time series Prometheus",
        "",
        "| phiên | dung lượng | nội dung |",
        "|---|---|---|",
    ]
    lines += table(runs)
    lines += [
        "",
        "```bash",
        f"aws s3 cp s3://{bucket}/runs/<phien>/probes.tgz . && tar xzf probes.tgz",
        "make availability PROBES=results/probe/      # dựng lại khoảng tin cậy",
        "```",
        "",
        "## `models/` và `datasets/`",
        "",
        f"{len(models)} tệp trọng số, {len(datasets)} tệp dataset. "
        "Trọng số là checkpoint chính chủ Qwen (FP16 và AWQ); dataset có checksum trong",
        "`bench/datasets/manifest-v1.json`.",
        "",
        "---",
        "",
        "## Truy cập",
        "",
        "Bucket **chặn toàn bộ public access** và tài khoản hiện chỉ có một IAM user",
        "(`mlops_lab`). Người thứ hai muốn đọc thì cần một IAM user riêng với quyền",
        "read-only trên bucket này — **chưa tạo**, và không nên dùng chung credential.",
        "",
        "Trong lúc chưa có, mọi kết luận và số liệu đều nằm trong git:",
        "`docs/acceptance-report.md`.",
    ]

    index = ROOT / "results" / "INDEX.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code, _ = sh(["aws", "s3", "cp", str(index), f"s3://{bucket}/INDEX.md"], timeout=180)
    if code != 0:
        print(f"  viet xong {index.relative_to(ROOT)} nhung upload that bai", file=sys.stderr)
        return 1
    print(f"  {len(audit)} tep audit, {len(runs)} tep runs")
    print(f"  -> s3://{bucket}/INDEX.md")
    print(f"  -> {index.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
