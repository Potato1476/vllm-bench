#!/usr/bin/env python3
"""Lexical vs dense vs fused, on the same 144 queries. Run under the torch venv.

    PYTHONPATH=. .venv-embed/bin/python -m rag.ablation

An ablation rather than a single number, because "hybrid is better" is not a finding --
which half carries which query type is. The hypothesis under test, from the lexical-only
baseline, is that BM25 handles `retrieval` queries (exact identifiers, R@10=0.932) and is
blind to `reasoning` ones (R@1=0.000, phrased as symptoms while the answers are phrased as
procedures). If that is right, dense should invert the pattern and the fusion should keep
both.

Query vectors are encoded once and reused across configurations, so the comparison is not
measuring how many times we called the model.
"""

from __future__ import annotations

import sys
import time

from prompt.canonical import canonicalise

from . import bm25, evaluate, policy as pol, retrieve
from .corpus import load_chunks, load_eval
from .dense import DenseIndex, SentenceTransformerBackend

INDEX_PATH = "data/index/dense-vi"


class _Cached:
    """A DenseRanker whose query vectors are already computed."""

    def __init__(self, index: DenseIndex, vecs: dict[str, object]):
        self.index = index
        self.vecs = vecs

    def rank(self, query: str, k: int):
        return self.index.search(self.vecs[query], k)


def _run(queries, index, by_id, dense, policy, top_k, canonical):
    rows, per_query, lat = [], [], []
    for q in queries:
        text = canonicalise(q.query).text if canonical else q.query
        t = time.perf_counter()
        hits = retrieve.hybrid(text, index, by_id, dense=dense,
                               candidates=50, top_k=top_k * 3)
        ranked = pol.apply([h.chunk for h in hits], policy).allowed[:top_k]
        lat.append((time.perf_counter() - t) * 1000)
        row = evaluate.score_one(ranked, q)
        rows.append(row)
        per_query.append((q, row))
    lat.sort()
    return evaluate.aggregate(rows), evaluate.by_query_type(per_query), lat


class _LexicalOff:
    """Dense-only: fuse a single ranking, so RRF degenerates to that ranking."""

    def __init__(self, inner):
        self.inner = inner

    def rank(self, query: str, k: int):
        return self.inner.rank(query, k)


def main() -> int:
    chunks = load_chunks()
    queries = load_eval()
    by_id = {c.chunk_id: c for c in chunks}
    lex = bm25.build([(c.chunk_id, c.text) for c in chunks])
    didx = DenseIndex.load(INDEX_PATH)
    policy = pol.Policy()

    print(f"  {len(chunks)} chunk, {len(queries)} truy van, dense dim="
          f"{didx.vectors.shape[1]}")

    backend = SentenceTransformerBackend()
    texts = sorted({canonicalise(q.query).text for q in queries}
                   | {q.query for q in queries})
    t0 = time.perf_counter()
    vecs = backend.encode(texts, is_query=True)
    enc_ms = (time.perf_counter() - t0) * 1000 / len(texts)
    cache = {t: v for t, v in zip(texts, vecs)}
    print(f"  ma hoa {len(texts)} truy van: {enc_ms:.0f} ms/truy van\n")

    dense = _Cached(didx, cache)
    empty = bm25.Bm25Index(doc_ids=[], doc_len=[], postings={}, avgdl=1.0)

    configs = [
        ("lexical",        lex,   None,                   False),
        ("lexical+canon",  lex,   None,                   True),
        ("dense",          empty, _LexicalOff(dense),     True),
        ("hybrid (RRF)",   lex,   dense,                  True),
    ]

    print(f"  {'cau hinh':16} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6} "
          f"{'nDCG':>6} {'truot':>6} {'p95 ms':>7}")
    detail = {}
    for name, li, de, canon in configs:
        m, bytype, lat = _run(queries, li, by_id, de, policy, 10, canon)
        detail[name] = bytype
        print(f"  {name:16} {m.recall_at[1]:6.3f} {m.recall_at[5]:6.3f} "
              f"{m.recall_at[10]:6.3f} {m.mrr10:6.3f} {m.ndcg10:6.3f} "
              f"{m.zero_hit:6} {lat[int(len(lat)*0.95)]:7.1f}")

    print(f"\n  {'theo loai':16} {'lexical':>18} {'dense':>18} {'hybrid':>18}")
    for qt in ("retrieval", "hybrid", "reasoning"):
        cells = []
        for name in ("lexical+canon", "dense", "hybrid (RRF)"):
            m = detail[name].get(qt)
            cells.append(f"R@1={m.recall_at[1]:.3f} R@10={m.recall_at[10]:.3f}"
                         if m else "-")
        print(f"  {qt:16} {cells[0]:>18} {cells[1]:>18} {cells[2]:>18}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
