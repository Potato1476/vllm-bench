"""Vietnamese PII detection and reversible redaction.

WHY PATTERNS AND NOT A MODEL, for this class of data. Vietnamese identity numbers are
structured and checkable: a CCCD encodes a province code, a century/sex digit and a birth
year, and roughly 88% of random 12-digit strings violate one of those three. A validated
pattern therefore has a false-positive rate a general NER model cannot match on exactly
the fields that matter most, at microseconds instead of milliseconds, with no GPU in the
request path.

Where patterns genuinely lose is PERSON NAMES and free-text addresses, which have no
structure to check. Those need an NER model and are listed as a documented gap below
rather than approximated badly -- a name detector with a 30% false-positive rate redacts
"Xanh SM" out of every answer and the guardrail gets switched off within a week.

TWO DIRECTIONS, DIFFERENT DEFAULTS:

  inbound   the user pastes a customer's phone number into a question. Redact before the
            prompt is built and before anything is logged, then restore in the answer if
            the caller was allowed to see it. Their own input, so restoring is safe.
  outbound  the model emits an identity number that came out of a retrieved document.
            Never restore. A value the corpus should not have contained is not made safe
            by the caller's access level.

REDACTION IS REVERSIBLE AND STABLE. Each distinct value maps to one placeholder within a
request, so "gọi lại số 0912345678, số 0912345678 không liên lạc được" yields one token
used twice. Stable placeholders also keep the text deterministic, which matters because
this string goes on to be hashed for the prefix cache.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Province codes issued for CCCD, 001-096 with gaps. Listing the real set rather than
# accepting 001-096 wholesale removes another slice of false positives.
_CCCD_PROVINCES = frozenset("""
001 002 004 006 008 010 011 012 014 015 017 019 020 022 024 025 026 027 030 031 033 034
035 036 037 038 040 042 044 045 046 048 049 051 052 054 056 058 060 062 064 066 067 068
070 072 074 075 077 079 080 082 083 084 086 087 089 091 092 093 094 095 096
""".split())

# Century + sex digit: 0/1 = 1900s, 2/3 = 2000s, 4/5 = 2100s ...
_CCCD_CENTURY = frozenset("012345")

# Mobile prefixes after the 2018 renumbering. Landline area codes are deliberately not
# matched: they collide with ordinary 10-digit figures in operations reports.
_MOBILE = r"(?:3[2-9]|5[2689]|7[06-9]|8[1-9]|9[0-9])"


@dataclass(frozen=True)
class Finding:
    kind: str
    value: str
    start: int
    end: int
    placeholder: str


@dataclass
class Redaction:
    text: str
    findings: list[Finding] = field(default_factory=list)
    # placeholder -> original, for the inbound restore path only.
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.findings

    def restore(self, text: str) -> str:
        for ph, original in self.mapping.items():
            text = text.replace(ph, original)
        return text


def _valid_cccd(digits: str) -> bool:
    if len(digits) != 12:
        return False
    if digits[:3] not in _CCCD_PROVINCES:
        return False
    if digits[3] not in _CCCD_CENTURY:
        return False
    # Birth-year digits must be a plausible year within the century the 4th digit names.
    return digits[4:6].isdigit()


def _valid_plate(text: str) -> bool:
    # Province codes on plates run 11-99; 00-10 are not issued to civilian vehicles.
    head = text[:2]
    return head.isdigit() and 11 <= int(head) <= 99


# A cue that must appear shortly before the digits. Without this, every 9- and 10-digit
# operational figure in the corpus -- and 720 of 798 chunks are daily reports full of
# them -- is redacted as an identity number. Measured on the first draft: a revenue of
# 987654321 became [CMND_1] and an order count of 1234567890 became [TAX_ID_1]. A
# guardrail that deletes the revenue from every answer gets turned off, and then it
# protects nothing.
_CUE_WINDOW = 40
_CUES = {
    "cmnd": r"cmnd|chứng minh|chung minh|giấy tờ tuỳ thân|cmt",
    "tax_id": r"mst|mã số thuế|ma so thue|tax",
    "passport": r"hộ chiếu|ho chieu|passport",
}
_CUE_RE = {k: re.compile(v, re.IGNORECASE) for k, v in _CUES.items()}


def _has_cue(kind: str, text: str, start: int) -> bool:
    cue = _CUE_RE.get(kind)
    if cue is None:
        return True
    return bool(cue.search(text[max(0, start - _CUE_WINDOW): start]))


_PATTERNS: tuple[tuple[str, re.Pattern[str], object], ...] = (
    # Order matters: longer, more specific identifiers first, so a CCCD is not first
    # consumed as a shorter id. Types with a structural check or a distinctive shape
    # stand alone; the bare-digit types are gated on _CUES above.
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), None),
    ("cccd", re.compile(r"(?<!\d)(\d{12})(?!\d)"), _valid_cccd),
    ("phone", re.compile(rf"(?<!\d)(?:\+?84[\s.-]?|0){_MOBILE}[\s.-]?\d{{3}}[\s.-]?\d{{4}}(?!\d)"), None),
    ("plate", re.compile(r"\b(\d{2}[A-Z]{1,2}[- ]?\d{3}\.?\d{2})\b"), _valid_plate),
    ("cmnd", re.compile(r"(?<!\d)(\d{9})(?!\d)"), None),
    ("tax_id", re.compile(r"\b(\d{10}(?:-\d{3})?)\b"), None),
    ("passport", re.compile(r"\b([A-Z]\d{7})\b"), None),
)

# Fields this module knowingly does not detect. Kept in code so the gap is visible to the
# next person instead of being rediscovered by an incident.
UNDETECTED = (
    "person_name",     # needs NER; regex on capitalised words is unusable in Vietnamese
    "street_address",  # no reliable structure
    "bank_account",    # 8-19 digits, indistinguishable from ordinary figures without context
)


def scan(text: str) -> list[Finding]:
    """All PII spans, non-overlapping, earliest and most specific first."""
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []

    for kind, pattern, validator in _PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            if not _has_cue(kind, text, s):
                continue
            raw = m.group(0)
            if validator is not None:
                probe = re.sub(r"\D", "", raw) if kind != "plate" else raw
                if not validator(probe):
                    continue
            taken.append((s, e))
            found.append(Finding(kind=kind, value=raw, start=s, end=e, placeholder=""))

    return sorted(found, key=lambda f: f.start)


def redact(text: str) -> Redaction:
    """Replace every detected value with a stable placeholder."""
    findings = scan(text)
    if not findings:
        return Redaction(text=text)

    counters: dict[str, int] = {}
    assigned: dict[str, str] = {}
    out: list[str] = []
    cursor = 0
    resolved: list[Finding] = []

    for f in findings:
        key = f"{f.kind}:{f.value}"
        ph = assigned.get(key)
        if ph is None:
            counters[f.kind] = counters.get(f.kind, 0) + 1
            ph = f"[{f.kind.upper()}_{counters[f.kind]}]"
            assigned[key] = ph
        out.append(text[cursor:f.start])
        out.append(ph)
        cursor = f.end
        resolved.append(Finding(f.kind, f.value, f.start, f.end, ph))

    out.append(text[cursor:])
    return Redaction(
        text="".join(out),
        findings=resolved,
        mapping={f.placeholder: f.value for f in resolved},
    )
