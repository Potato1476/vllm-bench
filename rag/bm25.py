"""BM25 Okapi over the Vietnamese term stream.

Written out rather than pulled from rank_bm25 for two reasons that matter here: the index
has to be serialisable to S3 alongside the corpus checksum so a retrieval result is
traceable to its index, and the scoring loop has to expose per-term contributions for the
explain output the guardrail layer uses to justify a citation.

Parameters are the Robertson/Sparck-Jones defaults, k1=1.2 b=0.75 (Robertson & Zaragoza,
"The Probabilistic Relevance Framework: BM25 and Beyond", 2009). b=0.75 is the right
choice for this corpus specifically: chunk length is near-constant at ~918 characters, so
length normalisation barely fires, and tuning it would be fitting noise.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .text_vi import lexical_terms

K1 = 1.2
B = 0.75


@dataclass
class Bm25Index:
    doc_ids: list[str] = field(default_factory=list)
    doc_len: list[int] = field(default_factory=list)
    # term -> list of (doc position, term frequency)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    avgdl: float = 0.0

    @property
    def n_docs(self) -> int:
        return len(self.doc_ids)

    def idf(self, term: str) -> float:
        """Robertson-Sparck-Jones IDF with the +0.5 smoothing, floored at zero.

        The floor matters: without it, a term appearing in more than half the corpus gets
        a negative weight, and a query containing one is actively penalised for matching.
        In this corpus 720 of 798 chunks are daily-operations reports sharing heavy
        boilerplate, so that case is not hypothetical.
        """
        df = len(self.postings.get(term, ()))
        if df == 0:
            return 0.0
        return max(0.0, math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5)))


def build(docs: list[tuple[str, str]]) -> Bm25Index:
    """docs: (doc_id, text). Returns an index ready to score."""
    idx = Bm25Index()
    for doc_id, text in docs:
        terms = lexical_terms(text)
        pos = len(idx.doc_ids)
        idx.doc_ids.append(doc_id)
        idx.doc_len.append(len(terms))
        for term, tf in Counter(terms).items():
            idx.postings.setdefault(term, []).append((pos, tf))
    idx.avgdl = (sum(idx.doc_len) / len(idx.doc_len)) if idx.doc_len else 0.0
    return idx


def search(idx: Bm25Index, query: str, k: int = 50) -> list[tuple[str, float]]:
    """Top-k (doc_id, score), descending. Ties broken by doc_id for reproducibility."""
    scores = _score_all(idx, query)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], idx.doc_ids[kv[0]]))
    return [(idx.doc_ids[p], s) for p, s in ranked[:k]]


def explain(idx: Bm25Index, query: str, doc_id: str, top_terms: int = 8
            ) -> list[tuple[str, float]]:
    """Which query terms earned this document its score, strongest first.

    The answer-grounding check cites a chunk; this says why the chunk was retrieved. When
    a citation looks wrong, this is the difference between "the retriever is broken" and
    "the retriever matched on a word the question did not mean".
    """
    try:
        pos = idx.doc_ids.index(doc_id)
    except ValueError:
        return []
    dl = idx.doc_len[pos]
    out: list[tuple[str, float]] = []
    for term in set(lexical_terms(query)):
        entry = idx.postings.get(term)
        if not entry:
            continue
        tf = next((f for p, f in entry if p == pos), 0)
        if tf:
            out.append((term, _term_score(idx.idf(term), tf, dl, idx.avgdl)))
    return sorted(out, key=lambda t: -t[1])[:top_terms]


def _term_score(idf: float, tf: int, dl: int, avgdl: float) -> float:
    denom = tf + K1 * (1.0 - B + B * (dl / avgdl if avgdl else 1.0))
    return idf * (tf * (K1 + 1.0)) / denom if denom else 0.0


def _score_all(idx: Bm25Index, query: str) -> dict[int, float]:
    scores: dict[int, float] = {}
    for term, qtf in Counter(lexical_terms(query)).items():
        entry = idx.postings.get(term)
        if not entry:
            continue
        idf = idx.idf(term)
        if idf <= 0.0:
            continue
        # Query term frequency is left un-normalised: queries here are one short sentence,
        # so a repeated term is a genuine emphasis signal rather than length noise.
        for pos, tf in entry:
            scores[pos] = scores.get(pos, 0.0) + qtf * _term_score(
                idf, tf, idx.doc_len[pos], idx.avgdl)
    return scores
