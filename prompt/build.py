"""Prompt assembly ordered for vLLM's prefix cache, and isolated by tenant.

HOW THE CACHE ACTUALLY WORKS, from the vLLM v0.29 design doc: the KV blocks of a request
are hashed as a chain -- each block's hash covers the parent hash plus the token ids in
that block -- and only FULL blocks are cached (16 tokens by default). Two requests share
cached computation for exactly as long as their token ids agree from position 0. One
differing token at position 3 discards everything after it.

Three consequences drive the layout below.

1. MOST STABLE CONTENT FIRST, most variable last.

       [ system rules           ]  identical for every request of this agent
       [ spotlight rule + nonce ]  ~fixed shape, nonce varies (see below)
       [ retrieved chunks       ]  varies with the question
       [ user question          ]  always different
                                   -> everything above the first difference is reused

   Putting the question first, which reads more naturally, would make the cache useless
   for every request.

2. CHUNKS ARE ORDERED BY DOCUMENT ID, NOT BY SCORE. This is the non-obvious one. Three
   paraphrases of one question usually retrieve the same chunk SET in different ORDERS,
   because the scores shift slightly. Sorted by score, the three prompts diverge at the
   first chunk and share nothing. Sorted by id, they are byte-identical up to the
   question. Relevance ordering inside the context is worth something to the model, but
   it is worth less than turning a full prefill into a cache hit, and the reranker has
   already done the job that matters by choosing WHICH chunks are present.

3. THE NONCE IS PER-SESSION, NOT PER-REQUEST. Spotlighting needs an unguessable delimiter,
   but a fresh nonce on every request sits above the chunks and destroys all reuse. A
   nonce rotated per session keeps both properties: a document written before the session
   started cannot contain it, and requests within the session share their prefix.

CACHE ISOLATION IS A SECURITY CONTROL HERE, not a performance detail. vLLM supports a
per-request `cache_salt` that is mixed into the first block's hash, so only requests with
the same salt can reuse each other's blocks. Two callers with different access levels must
never share KV state derived from documents only one of them may read, and the salt is
what enforces it. It is derived from the access level and the agent, never from the user
id -- salting per user would defeat caching entirely for no extra safety.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field

from guardrails.injection import spotlight_rule, wrap_untrusted
from rag.corpus import Chunk
from rag.text_vi import normalise

# Measured in this project with the Qwen2.5 tokenizer on Vietnamese text: 30.2 tokens per
# 100 characters, against 18.8 for English. Used only to report cache estimates in
# character terms; nothing functional depends on it.
VI_TOKENS_PER_CHAR = 0.302
BLOCK_TOKENS = 16


@dataclass(frozen=True)
class Session:
    """Everything that must stay constant for a run of requests to share cache."""

    agent: str
    access_level: str
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))

    @property
    def cache_salt(self) -> str:
        """Isolates KV reuse to callers with the same rights. See module docstring."""
        return hashlib.sha256(
            f"{self.agent}|{self.access_level}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class BuiltPrompt:
    system: str
    user: str
    cache_salt: str
    cited_ids: tuple[str, ...]
    stable_prefix_chars: int

    @property
    def approx_cacheable_blocks(self) -> int:
        tokens = int(self.stable_prefix_chars * VI_TOKENS_PER_CHAR)
        return tokens // BLOCK_TOKENS


_BASE_RULES = (
    "Bạn là trợ lý phân tích dữ liệu vận hành của Xanh SM. "
    "Chỉ trả lời dựa trên các tài liệu được cung cấp trong phần dữ liệu tham khảo. "
    "Nếu tài liệu không đủ căn cứ, hãy nói rõ là không đủ thông tin, không suy đoán. "
    "Mỗi khẳng định phải kèm mã tài liệu nguồn theo dạng [MÃ_TÀI_LIỆU]."
)


def build(
    question: str,
    chunks: list[Chunk],
    session: Session,
    *,
    advisory: str | None = None,
) -> BuiltPrompt:
    """Assemble the two messages. `advisory` carries the currency note from rag.policy."""
    # Stable region, in ascending order of how often it changes.
    parts = [_BASE_RULES, spotlight_rule(session.nonce)]
    if advisory:
        # Placed after the fixed rules so its presence or absence only invalidates the
        # cache from this point on, not from the top.
        parts.append(advisory)

    ordered = sorted(chunks, key=lambda c: (c.document_id, c.chunk_id))
    body = [
        f"[{c.document_id}]\n{wrap_untrusted(normalise(c.text), session.nonce)}"
        for c in ordered
    ]

    system = "\n\n".join(parts + ["## Dữ liệu tham khảo", "\n\n".join(body)])
    return BuiltPrompt(
        system=system,
        user=normalise(question),
        cache_salt=session.cache_salt,
        cited_ids=tuple(c.document_id for c in ordered),
        stable_prefix_chars=len(system),
    )


def shared_prefix_chars(a: str, b: str) -> int:
    """Length of the common leading run. The cache-hit proxy used by the analysis script."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i
