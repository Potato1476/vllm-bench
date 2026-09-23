"""Answer-side guardrail: is every claim actually supported by a retrieved document?

The brief requires answers that are grounded in sources. Two failures to catch, and they
need different checks:

  fabricated citation   the answer cites [METRIC-TRIP-009], which was never retrieved,
                        or does not exist. Purely structural, caught exactly.
  unsupported claim     the answer cites a real document, but says something the document
                        does not. Cannot be caught exactly without an entailment model.

The structural check runs first and costs nothing. The lexical overlap check below is a
SCREEN, not a verdict: it measures how much of the answer's content vocabulary appears in
the cited chunks, and flags answers that drift far from their sources. It cannot tell
"doanh thu tăng 5%" from "doanh thu giảm 5%" -- those share every content word. Anything
claiming to catch that needs NLI, and the honest place for that is a second model call,
which is a week-4 decision with a latency budget attached, not something to fake here
with a threshold.

So the contract is: BLOCK on fabricated citations, FLAG on low overlap, and never claim
the flag means the answer is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag.corpus import Chunk
from rag.text_vi import syllables

CITATION = re.compile(r"\[([A-Z][A-Z0-9-]{2,})\]")

# Vietnamese function words carry no evidence. Overlap computed on them would make any
# fluent answer look grounded.
_STOP = frozenset("""
và là của có được cho khi nào thì mà với các những một này đó đây kia ở từ đến theo
trong ngoài trên dưới về bị bởi do nếu hoặc hay nhưng còn đã sẽ đang rất quá cũng
không phải như để ra vào lên xuống sau trước nữa lại chỉ đều tất cả mọi
""".split())


@dataclass
class GroundingReport:
    cited: tuple[str, ...]
    fabricated: tuple[str, ...]
    uncited_sentences: tuple[str, ...]
    overlap: float
    verdict: str            # "ok" | "flag" | "block"
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


def check(answer: str, context: list[Chunk], *,
          min_overlap: float = 0.35,
          require_citation_per_sentence: bool = True) -> GroundingReport:
    available = {c.document_id for c in context}
    cited = tuple(dict.fromkeys(CITATION.findall(answer)))
    fabricated = tuple(c for c in cited if c not in available)

    # Only the chunks the answer actually leans on count as its evidence base.
    used = [c for c in context if c.document_id in set(cited)] or context
    evidence = {t for c in used for t in syllables(c.text)} - _STOP

    content = [t for t in syllables(CITATION.sub("", answer)) if t not in _STOP]
    overlap = (sum(1 for t in content if t in evidence) / len(content)) if content else 1.0

    uncited: list[str] = []
    if require_citation_per_sentence:
        for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
            s = sentence.strip()
            if len(s) < 25 or not s:
                continue
            # A sentence that asserts something must name where it came from.
            if not CITATION.search(s) and not _is_hedge(s):
                uncited.append(s)

    notes: list[str] = []
    verdict = "ok"
    if fabricated:
        verdict = "block"
        notes.append(f"trích dẫn không có trong ngữ cảnh: {', '.join(fabricated)}")
    # The citation requirement applies to ASSERTIONS. An answer that declines -- "the
    # documents do not cover this" -- asserts nothing about the world and has nothing to
    # cite. Checking `cited` before `is_refusal` blocked exactly the behaviour the system
    # prompt asks for, which would have taught the model that hedging is punished and
    # guessing is not.
    elif not cited and content and not _is_refusal(answer):
        verdict = "block"
        notes.append("câu trả lời không trích dẫn tài liệu nào")
    if verdict != "block":
        if overlap < min_overlap:
            verdict = "flag"
            notes.append(f"trùng từ vựng với nguồn thấp ({overlap:.0%})")
        if uncited:
            verdict = "flag"
            notes.append(f"{len(uncited)} câu khẳng định không kèm trích dẫn")

    return GroundingReport(cited=cited, fabricated=fabricated,
                           uncited_sentences=tuple(uncited), overlap=overlap,
                           verdict=verdict, notes=notes)


_HEDGE = re.compile(
    r"(không đủ thông tin|không tìm thấy|tài liệu không|không có căn cứ|"
    r"cần kiểm tra thêm|tôi không thể)", re.IGNORECASE)


def _is_hedge(sentence: str) -> bool:
    """A refusal is the one assertion that needs no source."""
    return bool(_HEDGE.search(sentence))


def _is_refusal(answer: str) -> bool:
    """True when every substantive sentence declines rather than asserts.

    Deliberately requires ALL of them: an answer that states three facts and then adds
    "cần kiểm tra thêm" is not a refusal, and its three facts still need sources.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer.strip())
                 if len(s.strip()) >= 15]
    return bool(sentences) and all(_is_hedge(s) for s in sentences)
