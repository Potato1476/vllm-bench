#!/usr/bin/env python3
"""Measure retrieval on the gold set. No GPU, no network, no cluster.

    python3 -m rag.run_eval                 # lexical baseline + policy
    python3 -m rag.run_eval --no-policy     # what the traps do when nothing stops them
"""

from __future__ import annotations

import argparse
import sys
import time

from . import bm25, evaluate, policy as pol, retrieve
from .corpus import load_chunks, load_eval


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--candidates", type=int, default=50)
    ap.add_argument("--no-policy", action="store_true",
                    help="skip the metadata layer, to show what it is worth")
    ap.add_argument("--access", default="internal-demo")
    a = ap.parse_args()

    chunks = load_chunks()
    queries = load_eval()
    by_id = {c.chunk_id: c for c in chunks}
    print(f"  corpus {len(chunks)} chunk, {len(queries)} truy van, "
          f"{len({q.base_question_id for q in queries})} cau hoi goc")
    n_stale = sum(1 for c in chunks if not c.is_current)
    print(f"  tai lieu khong con hieu luc trong corpus: {n_stale}")

    t0 = time.perf_counter()
    index = bm25.build([(c.chunk_id, c.text) for c in chunks])
    t_index = time.perf_counter() - t0
    print(f"  index {index.n_docs} doc, {len(index.postings)} term, "
          f"avgdl={index.avgdl:.0f}, dung {t_index*1000:.0f} ms\n")

    policy = pol.Policy(access_level=a.access, require_current=not a.no_policy)

    per_query = []
    lat = []
    for q in queries:
        t = time.perf_counter()
        hits = retrieve.hybrid(q.query, index, by_id, dense=None,
                               candidates=a.candidates, top_k=a.top_k * 3)
        result = pol.apply([h.chunk for h in hits], policy)
        ranked = result.allowed[: a.top_k] if not a.no_policy else \
            [h.chunk for h in hits][: a.top_k]
        lat.append((time.perf_counter() - t) * 1000)
        per_query.append((q, evaluate.score_one(ranked, q)))

    rows = [r for _, r in per_query]
    overall = evaluate.aggregate(rows)
    mode = "KHONG policy" if a.no_policy else f"policy={a.access}"
    print(f"  === Tong ({mode}) ===")
    print(overall.render("tat ca"))
    print()
    print("  === Theo loai truy van ===")
    for t, m in evaluate.by_query_type(per_query).items():
        print(m.render(t))
    print()
    print(f"  do lech nDCG giua cac cach dien dat cung mot cau hoi: "
          f"{evaluate.paraphrase_spread(per_query):.3f}")
    lat.sort()
    print(f"  do tre truy hoi: p50={lat[len(lat)//2]:.1f} ms  "
          f"p95={lat[int(len(lat)*0.95)]:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
