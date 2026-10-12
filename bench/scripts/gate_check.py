#!/usr/bin/env python3
"""Chấm kết quả quét theo bộ quy tắc của team Deployment Quality Gate.

    gate_check.py --gate <thu-muc-gate-da-clone> \
        --kyverno out/kyverno.txt --conftest out/conftest.json \
        --trivy-secret out/secret.json [--trivy-vuln out/vuln.json] \
        --summary "$GITHUB_STEP_SUMMARY"

VÌ SAO TÁCH "CHẠY CÔNG CỤ" KHỎI "PHÁN XÉT"

Workflow chạy trivy/conftest/kyverno rồi ghi output thô ra file; file này đọc output, gán
rule id, tra mức từ `security/muc-rule.json` CỦA GATE, áp ngoại lệ của DA#51, rồi quyết
định đỏ hay xanh. Nhờ tách như vậy, phần quyết định — phần dễ sai nhất — chạy được trong
unittest trên laptop, không cần docker, không cần mạng, không cần cụm.

MỨC CHẶN KHÔNG ĐƯỢC CHÉP LẠI Ở ĐÂY

Đọc trực tiếp `security/muc-rule.json` trong bản gate đã ghim. Giữ một bản sao trong repo
này nghĩa là tuần sau gate chuyển một rule sang block mà DA#51 vẫn chấm theo mức cũ, và
"check xanh" sẽ có nghĩa khác với bên kia — đúng cái mà việc tái dùng policy nhằm tránh.

HARD GATE KHÔNG NGOẠI LỆ ĐƯỢC

Gate quy định IMG-001, K8S-001..003, K8S-007, ML-002 "luôn BLOCK, không file ghi đè nào hạ
xuống được". File này từ chối ngay khi gate-exceptions.yaml khai một trong số đó — từ chối
cả lượt chạy, chứ không bỏ qua dòng khai sai, vì một ngoại lệ bị bỏ qua im lặng trông y như
một ngoại lệ được chấp nhận.

NGOẠI LỆ HẾT HẠN LÀ KHÔNG CÒN NGOẠI LỆ

Hết ngày `expires` thì vi phạm chặn lại như thường và dòng đó được in ra là ĐÃ HẾT HẠN. Một
ngoại lệ không hạn là một quy tắc đã bị tắt bằng cách khác.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

RULE_RE = re.compile(r"\b(IMG|K8S|ML)-\d{3}\b")
HARD_GATE_FALLBACK = ("IMG-001", "K8S-001", "K8S-002", "K8S-003", "K8S-007", "ML-002")


class GateError(ValueError):
    """Cấu hình sai đến mức không chấm được."""


@dataclass(frozen=True)
class Finding:
    rule: str
    resource: str
    detail: str = ""
    source: str = ""


@dataclass(frozen=True)
class Exception_:
    rule: str
    resources: tuple[str, ...]
    owner: str
    expires: date
    reason: str

    def covers(self, finding: Finding) -> bool:
        if self.rule != finding.rule:
            return False
        if not self.resources:
            return True
        return any(fnmatch.fnmatch(finding.resource, pat) for pat in self.resources)


@dataclass
class Levels:
    hard_gate: frozenset[str]
    muc: dict[str, str]

    def of(self, rule: str) -> str:
        # Mặc định `warn`, đúng như ghi chú trong muc-rule.json: "Rule không có tên ở đây
        # = warn". Một rule mới bên gate vì thế không làm CI đỏ đột ngột, nhưng vẫn hiện ra.
        return "hard_gate" if rule in self.hard_gate else self.muc.get(rule, "warn")

    def blocking(self, rule: str) -> bool:
        return self.of(rule) in ("hard_gate", "block")


def load_levels(gate: Path) -> Levels:
    path = gate / "security" / "muc-rule.json"
    if not path.is_file():
        raise GateError(
            f"khong thay {path} -- gate da doi cau truc? Kiem lai GATE_REF truoc khi "
            "sua file nay, vi muc chan phai doc tu gate chu khong chep lai.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    hard = frozenset(raw.get("hard_gate") or HARD_GATE_FALLBACK)
    muc = {k: str(v).lower() for k, v in (raw.get("muc") or {}).items()}
    return Levels(hard, muc)


def load_exceptions(path: Path, levels: Levels) -> list[Exception_]:
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Exception_] = []
    for i, entry in enumerate(raw.get("exceptions") or []):
        if not isinstance(entry, dict):
            raise GateError(f"exceptions[{i}] phai la mot anh xa")
        rule = str(entry.get("rule") or "")
        if not RULE_RE.fullmatch(rule):
            raise GateError(f"exceptions[{i}].rule khong hop le: {rule!r}")
        if rule in levels.hard_gate:
            raise GateError(
                f"exceptions[{i}]: {rule} la hard gate -- gate quy dinh khong file ghi de "
                "nao ha xuong duoc. Xoa dong nay; neu that su can thi phai sua o phia gate.")
        for khoa in ("owner", "reason", "expires"):
            if not entry.get(khoa):
                raise GateError(f"exceptions[{i}] ({rule}) thieu {khoa}")
        expires = entry["expires"]
        if not isinstance(expires, date):
            try:
                expires = date.fromisoformat(str(expires))
            except ValueError as exc:
                raise GateError(f"exceptions[{i}].expires phai la YYYY-MM-DD: {exc}") from exc
        res = entry.get("resources") or []
        if not isinstance(res, list) or not all(isinstance(r, str) for r in res):
            raise GateError(f"exceptions[{i}].resources phai la danh sach chuoi")
        out.append(Exception_(rule, tuple(res), str(entry["owner"]), expires,
                              " ".join(str(entry["reason"]).split())))
    return out


# ------------------------------------------------------------------ đọc output công cụ


def parse_kyverno(text: str) -> list[Finding]:
    """`kyverno apply` ở dạng văn bản. Không có -o json: bản CLI 1.19.1 không nhận cờ đó,
    và dò theo dòng `policy ... -> resource ... failed:` là hợp đồng ổn định hơn một cờ
    chưa chắc tồn tại ở bản sau."""
    out = []
    for m in re.finditer(r"^policy (\S+) -> resource (\S+) failed:", text, re.M):
        pol, res = m.group(1), m.group(2)
        k = re.match(r"(ml|k8s|img)-(\d{3})", pol)
        rule = f"{k.group(1).upper()}-{k.group(2)}" if k else pol.upper()
        # namespace/Kind/ten -> Kind/ten, de glob trong gate-exceptions.yaml doc duoc
        short = res.split("/", 1)[1] if res.count("/") >= 2 else res
        out.append(Finding(rule, short, f"policy {pol}", "kyverno"))
    return out


def parse_conftest(payload: object) -> list[Finding]:
    out = []
    for block in payload if isinstance(payload, list) else []:
        # Conftest in ra duong dan BEN TRONG container (/src/...). Bo tien to di de
        # glob trong gate-exceptions.yaml viet theo duong dan trong repo, giong hon.
        target = re.sub(r"^/src/", "", str(block.get("filename") or "?"))
        for kind in ("failures", "warnings"):
            for item in block.get(kind) or []:
                msg = str(item.get("msg") or "")
                m = RULE_RE.search(msg)
                out.append(Finding(m.group(0) if m else "IMG-000", target, msg, "conftest"))
    return out


def parse_trivy_secret(payload: object) -> list[Finding]:
    """Mọi secret lộ ra đều là IMG-001, và IMG-001 là hard gate."""
    out = []
    if not isinstance(payload, dict):
        return out
    for res in payload.get("Results") or []:
        for s in res.get("Secrets") or []:
            out.append(Finding("IMG-001", str(res.get("Target") or "?"),
                               f"{s.get('RuleID')} dong {s.get('StartLine')}", "trivy-secret"))
    return out


def parse_trivy_vuln(payload: object, min_severity=("CRITICAL", "HIGH")) -> list[Finding]:
    """IMG-002 CVE có bản vá, IMG-003 chênh so với baseline (ở đây chỉ IMG-002).

    Chỉ tính CVE **có bản vá**: một CRITICAL chưa ai vá được thì chặn merge không làm nó
    biến mất, nó chỉ làm cả đội học cách bỏ qua màu đỏ.
    """
    out = []
    if not isinstance(payload, dict):
        return out
    for res in payload.get("Results") or []:
        for v in res.get("Vulnerabilities") or []:
            if str(v.get("Severity", "")).upper() not in min_severity:
                continue
            if not v.get("FixedVersion"):
                continue
            out.append(Finding("IMG-002", str(res.get("Target") or "?"),
                               f"{v.get('VulnerabilityID')} {v.get('PkgName')} "
                               f"{v.get('InstalledVersion')} -> {v.get('FixedVersion')}",
                               "trivy-vuln"))
    return out


def read_json(path: Path | None) -> object:
    if path is None or not path.is_file() or not path.read_text(encoding="utf-8").strip():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------ chấm


@dataclass
class Verdict:
    enforced: list[tuple[Finding, str]] = field(default_factory=list)
    excused: list[tuple[Finding, Exception_]] = field(default_factory=list)
    expired: list[tuple[Finding, Exception_]] = field(default_factory=list)
    informational: list[tuple[Finding, str]] = field(default_factory=list)
    unused: list[Exception_] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.enforced and not self.expired


def judge(findings: list[Finding], levels: Levels, exceptions: list[Exception_],
          today: date) -> Verdict:
    v = Verdict()
    used: set[int] = set()
    for f in findings:
        level = levels.of(f.rule)
        match = next(((i, e) for i, e in enumerate(exceptions) if e.covers(f)), None)
        if match is not None:
            i, exc = match
            used.add(i)
            # Hard gate không bao giờ tới được đây: load_exceptions đã từ chối từ đầu.
            if exc.expires < today:
                v.expired.append((f, exc))
            else:
                v.excused.append((f, exc))
            continue
        if levels.blocking(f.rule):
            v.enforced.append((f, level))
        else:
            v.informational.append((f, level))
    v.unused = [e for i, e in enumerate(exceptions) if i not in used]
    return v


def render(v: Verdict, levels: Levels, gate_ref: str) -> str:
    L = [f"## Quality Gate — policy ghim tại `{gate_ref}`", ""]
    if v.ok and not v.informational:
        L += ["Không vi phạm nào. Mọi quy tắc chặn đều đạt.", ""]
    L += [f"- **chặn:** {len(v.enforced)}",
          f"- **ngoại lệ hết hạn (vẫn chặn):** {len(v.expired)}",
          f"- có ngoại lệ còn hiệu lực: {len(v.excused)}",
          f"- chỉ báo (warn/info): {len(v.informational)}", ""]
    if v.enforced:
        L += ["### Chặn — phải sửa", "", "| Rule | Mức | Tài nguyên | Chi tiết |", "|---|---|---|---|"]
        for f, lv in v.enforced:
            L.append(f"| {f.rule} | {lv} | `{f.resource}` | {f.detail[:160]} |")
        L.append("")
    if v.expired:
        L += ["### Ngoại lệ ĐÃ HẾT HẠN — chặn lại", "",
              "| Rule | Tài nguyên | Hết hạn | Người chịu trách nhiệm |", "|---|---|---|---|"]
        for f, e in v.expired:
            L.append(f"| {f.rule} | `{f.resource}` | {e.expires} | {e.owner} |")
        L += ["", "Gia hạn thì phải sửa `security/gate-exceptions.yaml` qua PR, kèm lý do "
              "còn đúng.", ""]
    if v.excused:
        L += ["<details><summary>Ngoại lệ còn hiệu lực</summary>", "",
              "| Rule | Tài nguyên | Hết hạn | Lý do |", "|---|---|---|---|"]
        for f, e in sorted(v.excused, key=lambda x: (x[0].rule, x[0].resource)):
            L.append(f"| {f.rule} | `{f.resource}` | {e.expires} | {e.reason[:180]} |")
        L += ["", "</details>", ""]
    if v.informational:
        L += ["<details><summary>Chỉ báo</summary>", "", "| Rule | Mức | Tài nguyên |",
              "|---|---|---|"]
        for f, lv in sorted(v.informational, key=lambda x: (x[0].rule, x[0].resource)):
            L.append(f"| {f.rule} | {lv} | `{f.resource}` |")
        L += ["", "</details>", ""]
    if v.unused:
        L += ["### Ngoại lệ không còn vi phạm nào khớp", "",
              "Xoá khỏi `security/gate-exceptions.yaml` — một ngoại lệ không dùng là một "
              "quy tắc đang tắt mà không ai cần nó tắt.", ""]
        for e in v.unused:
            L.append(f"- **{e.rule}** {list(e.resources) or 'moi tai nguyen'} (hết hạn {e.expires})")
        L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gate", type=Path, required=True, help="thu muc gate da clone")
    ap.add_argument("--exceptions", type=Path,
                    default=Path(__file__).resolve().parents[2] / "security" / "gate-exceptions.yaml")
    ap.add_argument("--kyverno", type=Path)
    ap.add_argument("--conftest", type=Path)
    ap.add_argument("--trivy-secret", type=Path)
    ap.add_argument("--trivy-vuln", type=Path)
    ap.add_argument("--summary", type=Path, help="ghi bang markdown ra day (GITHUB_STEP_SUMMARY)")
    ap.add_argument("--gate-ref", default="?")
    ap.add_argument("--today", help="YYYY-MM-DD, de test")
    ap.add_argument("--list-exceptions", action="store_true",
                    help="liet ke ngoai le theo ngay het han roi thoat")
    a = ap.parse_args(argv)

    try:
        if a.list_exceptions and not (a.gate / "security" / "muc-rule.json").is_file():
            # Chi de LIET KE. Danh sach hard gate du phong vua du de tu choi mot dong khai
            # sai, va viec liet ke khong quyet dinh do/xanh. Khi CHAM thi bat buoc doc muc
            # that tu gate -- load_levels se bao loi neu khong co.
            levels = Levels(frozenset(HARD_GATE_FALLBACK), {})
        else:
            levels = load_levels(a.gate)
        exceptions = load_exceptions(a.exceptions, levels)
    except (GateError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"  LOI CAU HINH: {exc}", file=sys.stderr)
        return 2

    today = date.fromisoformat(a.today) if a.today else date.today()

    if a.list_exceptions:
        if not exceptions:
            print("  khong co ngoai le nao")
            return 0
        for e in sorted(exceptions, key=lambda x: x.expires):
            con = (e.expires - today).days
            trang = f"{con} ngay nua" if con >= 0 else f"HET HAN {-con} ngay"
            print(f"  {e.rule:9} {e.expires}  {trang:20} {e.owner}")
            print(f"            {', '.join(e.resources) or 'moi tai nguyen'}")
        soon = [e for e in exceptions if 0 <= (e.expires - today).days <= 14]
        if soon:
            print(f"\n  {len(soon)} ngoai le het han trong 14 ngay toi -- gia han phai sua "
                  "security/gate-exceptions.yaml qua PR, kem ly do con dung.")
        return 0

    findings: list[Finding] = []
    if a.kyverno and a.kyverno.is_file():
        findings += parse_kyverno(a.kyverno.read_text(encoding="utf-8"))
    findings += parse_conftest(read_json(a.conftest))
    findings += parse_trivy_secret(read_json(a.trivy_secret))
    findings += parse_trivy_vuln(read_json(a.trivy_vuln))

    v = judge(findings, levels, exceptions, today)
    report = render(v, levels, a.gate_ref)
    print(report)
    if a.summary:
        with a.summary.open("a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    if not v.ok:
        print(f"\n  TRUOT: {len(v.enforced)} vi pham chan, "
              f"{len(v.expired)} ngoai le het han.", file=sys.stderr)
        return 1
    print(f"\n  DAT: {len(v.excused)} ngoai le con hieu luc, "
          f"{len(v.informational)} chi bao.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
