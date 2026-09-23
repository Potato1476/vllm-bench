"""Hybrid retrieval: lexical + dense, fused with Reciprocal Rank Fusion.

WHY RRF AND NOT A WEIGHTED SCORE SUM. BM25 scores are unbounded and corpus-dependent;
cosine similarities sit in [-1, 1] and bunch up near the top. Adding them requires
normalising two distributions that change shape with every query, and the mixing weight
then needs tuning per corpus. RRF (Cormack, Clarke & Buettcher, SIGIR 2009) throws away
the scores and fuses ranks:

    score(d) = sum over rankers of  1 / (k + rank(d))

It has no per-corpus tuning, it is robust when one ranker is badly wrong for a query, and
in the original paper it beat every individual system and the tuned score-combination
baselines. k=60 is their value; it controls how quickly the contribution decays with rank.

WHY BOTH RANKERS ARE NEEDED HERE, concretely. This corpus is full of exact identifiers --
trip_status, pickup_zone_id, fact_bookings, METRIC-TRIP-001 -- and a schema question is
answered by the chunk containing the literal table name. Dense retrieval is famously
unreliable at exact token match. Meanwhile 720 of 798 chunks are daily-operations reports
with near-identical boilerplate, where a paraphrased question ("cho tôi biết:", "theo định
nghĩa hiện hành,") shares few content words with the target, and lexical search drifts.
Each ranker fails where the other holds.

THE DENSE SIDE IS OPTIONAL AND RUNS ELSEWHERE. Encoding queries with a 0.6B model belongs
on the GPU node beside vLLM, not in this process. `hybrid` takes whatever dense ranking it
is given and degrades to pure lexical when handed none, so the pipeline is testable and
measurable without a GPU, which is also how it gets a baseline to prove the dense side
earns its cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from . import bm25
from .corpus import Chunk

RRF_K = 60


class DenseRanker(Protocol):
    """Anything that can rank chunk ids for a query.

    Implemented in the cluster by an embedding service; left unimplemented here on
    purpose. See docs in this module for why.
    """

    def rank(self, query: str, k: int) -> list[tuple[str, float]]:
        ...


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    rank: int
    fused_score: float
    lexical_rank: int | None
    dense_rank: int | None

    @property
    def from_both(self) -> bool:
        return self.lexical_rank is not None and self.dense_rank is not None


def rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked id lists. Absent from a list simply contributes nothing."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


def hybrid(
    query: str,
    index: bm25.Bm25Index,
    chunks_by_id: dict[str, Chunk],
    dense: DenseRanker | None = None,
    *,
    candidates: int = 50,
    top_k: int = 10,
) -> list[Hit]:
    """Retrieve top_k chunks. Pure lexical when `dense` is None."""
    lex = [doc_id for doc_id, _ in bm25.search(index, query, k=candidates)]
    lex_pos = {d: i + 1 for i, d in enumerate(lex)}

    rankings: list[Sequence[str]] = [lex]
    dense_pos: dict[str, int] = {}
    if dense is not None:
        dn = [doc_id for doc_id, _ in dense.rank(query, candidates)]
        dense_pos = {d: i + 1 for i, d in enumerate(dn)}
        rankings.append(dn)

    fused = rrf(rankings)
    # Sort by fused score, then by id so a tie is reproducible across runs and machines.
    order = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))

    hits: list[Hit] = []
    for i, (doc_id, score) in enumerate(order[:top_k], start=1):
        chunk = chunks_by_id.get(doc_id)
        if chunk is None:
            continue
        hits.append(Hit(
            chunk=chunk,
            rank=i,
            fused_score=score,
            lexical_rank=lex_pos.get(doc_id),
            dense_rank=dense_pos.get(doc_id) or None,
        ))
    return hits
