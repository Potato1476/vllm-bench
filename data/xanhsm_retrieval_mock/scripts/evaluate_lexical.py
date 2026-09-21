#!/usr/bin/env python3
"""Dependency-free BM25 smoke test for the mock retrieval dataset."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def main() -> None:
    docs = [d for d in load_jsonl(ROOT / "corpus/chunks.jsonl") if d["status"] == "active"]
    queries = load_jsonl(ROOT / "eval/retrieval_eval.jsonl")
    tokenized = [tokens(d["title"] + " " + d["text"] + " " + " ".join(d["tags"])) for d in docs]
    lengths = [len(t) for t in tokenized]
    avg_len = sum(lengths) / len(lengths)
    df = Counter()
    for item in tokenized:
        df.update(set(item))
    n_docs = len(docs)
    k1, b = 1.5, 0.75

    def rank(query: str) -> list[str]:
        q_terms = set(tokens(query))
        scored = []
        for index, item in enumerate(tokenized):
            tf = Counter(item)
            score = 0.0
            for term in q_terms:
                if term not in tf:
                    continue
                idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                denom = tf[term] + k1 * (1 - b + b * lengths[index] / avg_len)
                score += idf * tf[term] * (k1 + 1) / denom
            scored.append((score, docs[index]["document_id"]))
        scored.sort(reverse=True)
        return [doc_id for _, doc_id in scored]

    hits = {1: 0, 3: 0, 5: 0}
    reciprocal_rank = 0.0
    failures = []
    for query in queries:
        ranked = rank(query["query"])
        relevant = set(query["relevant_document_ids"])
        first = next((idx + 1 for idx, doc_id in enumerate(ranked) if doc_id in relevant), None)
        if first:
            reciprocal_rank += 1 / first
        for cutoff in hits:
            if relevant.intersection(ranked[:cutoff]):
                hits[cutoff] += 1
        if not relevant.intersection(ranked[:5]):
            failures.append({"query_id": query["query_id"], "query": query["query"], "top5": ranked[:5], "expected": sorted(relevant)})

    total = len(queries)
    result = {
        "engine": "stdlib-bm25-smoke-test",
        "active_documents": n_docs,
        "queries": total,
        "recall_at_1": round(hits[1] / total, 4),
        "recall_at_3": round(hits[3] / total, 4),
        "recall_at_5": round(hits[5] / total, 4),
        "mrr": round(reciprocal_rank / total, 4),
        "top5_failures": failures[:20],
        "failure_count": len(failures),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
