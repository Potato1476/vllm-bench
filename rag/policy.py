"""Metadata policy: who may see a chunk, and whether it is still in force.

This runs BETWEEN retrieval and prompt assembly, and it is the part of the RAG pipeline
that no amount of embedding quality can replace.

TWO SEPARATE DECISIONS, deliberately not merged:

  access     may this caller see this chunk at all?      -> hard drop, never mentioned
  currency   is this definition still in force?          -> withheld, but acknowledged

Access is a security boundary. A chunk the caller cannot see must not influence the
answer, must not appear in a citation, and must not be hinted at -- "there is a document
I cannot show you" is itself a leak about what exists.

Currency is an accuracy boundary and the opposite. When a superseded definition is
suppressed, the answer should say so and name the replacement. A user asking "how is
Active Driver defined" after the 2026-01-01 change deserves to hear that the rule
changed, not to receive the new rule as if it had always been the rule. Silently dropping
OLD-METRIC-ACTIVE-2025 produces a technically correct answer that leaves the user unable
to reconcile it with last year's report.

That distinction is why `apply` returns suppressed items with reasons instead of a
filtered list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .corpus import Chunk

# Ordered from least to most privileged. A grant implies everything below it.
ACCESS_ORDER = ("public", "internal-demo")


@dataclass(frozen=True)
class Policy:
    """What a caller is allowed to see, and how strict to be about currency."""

    access_level: str = "internal-demo"
    # Superseded and unapproved definitions never enter the answer context by default.
    # Turn off only for a question explicitly about history ("what was the old rule").
    require_current: bool = True

    def grants(self) -> frozenset[str]:
        try:
            upto = ACCESS_ORDER.index(self.access_level)
        except ValueError:
            return frozenset({"public"})
        return frozenset(ACCESS_ORDER[: upto + 1])


@dataclass(frozen=True)
class Suppressed:
    chunk: Chunk
    reason: str          # "deprecated" | "draft"
    supersedes_note: str  # human-readable, safe to show the user


@dataclass(frozen=True)
class PolicyResult:
    allowed: list[Chunk]
    suppressed: list[Suppressed]
    # Count only -- never the ids or the topics. See the access note above.
    denied_count: int

    @property
    def has_stale_alternative(self) -> bool:
        return bool(self.suppressed)


_REASON_NOTE = {
    "deprecated": (
        "Có một định nghĩa cũ về chủ đề này đã HẾT HIỆU LỰC và không được dùng cho "
        "báo cáo hiện hành."
    ),
    "draft": (
        "Có một BẢN NHÁP về chủ đề này chưa được phê duyệt và không được dùng làm "
        "căn cứ."
    ),
}


def apply(chunks: list[Chunk], policy: Policy) -> PolicyResult:
    allowed: list[Chunk] = []
    suppressed: list[Suppressed] = []
    denied = 0
    grants = policy.grants()

    for c in chunks:
        if c.access_level not in grants:
            denied += 1
            continue
        if policy.require_current and not c.is_current:
            reason = c.status if c.status in _REASON_NOTE else "deprecated"
            suppressed.append(Suppressed(c, reason, _REASON_NOTE[reason]))
            continue
        allowed.append(c)

    return PolicyResult(allowed=allowed, suppressed=suppressed, denied_count=denied)


def currency_advisory(result: PolicyResult) -> str | None:
    """One line to append to the system prompt when stale matches were withheld.

    Kept short and generic on purpose: it must not restate the stale content, or the
    model will have the superseded definition in context after all.
    """
    if not result.suppressed:
        return None
    reasons = sorted({s.reason for s in result.suppressed})
    return " ".join(_REASON_NOTE[r] for r in reasons) + (
        " Chỉ trả lời theo các tài liệu đang có hiệu lực được cung cấp, và nói rõ với "
        "người dùng rằng định nghĩa đã thay đổi nếu điều đó liên quan tới câu hỏi."
    )
