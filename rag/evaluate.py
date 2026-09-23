"""Retrieval evaluation against the 144 gold queries.

THE STANDARD METRICS, and what each one is here to catch:

  Recall@k   did the gold chunks make it into the context window at all? This is the
             ceiling on answer quality -- nothing the generator does can recover a chunk
             that was never retrieved. The hybrid and reasoning queries have 4 gold
             chunks each, so Recall@5 is a genuinely hard bar and Recall@10 is the one
             that maps to a realistic context budget.
  MRR@10     how far down the list is the FIRST gold chunk? Proxy for whether the top of
             the prompt is relevant, which is where models attend most.
  nDCG@10    rank-weighted, handles the multi-gold queries properly. Recall alone treats
             "all 4 at ranks 1-4" the same as "all 4 at ranks 7-10".

THE METRIC THAT IS NOT STANDARD, and matters more than any of the above for this project:

  trap_rate  how often a deprecated or draft chunk reaches the answer context.

A system can score well on all three metrics above while confidently citing the 2025
Active Driver definition, because that chunk IS topically relevant -- it is simply no
longer true. Recall cannot see the difference; only the metadata can. Reporting trap_rate
next to Recall is what stops the policy layer from being quietly deleted by someone
optimising nDCG.

Queries are grouped by base_question_id when reporting spread, because Q01-V1/V2/V3 are
three paraphrases of one question. Averaging over all 144 without noting that would
overstate independence: there are 48 distinct questions, not 144.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .corpus import Chunk, EvalQuery


@dataclass
class Metrics:
    n: int = 0
    recall_at: dict[int, float] = field(default_factory=dict)
    mrr10: float = 0.0
    ndcg10: float = 0.0
    trap_rate: float = 0.0
    zero_hit: int = 0

    def render(self, title: str) -> str:
        ks = " ".join(f"R@{k}={self.recall_at[k]:.3f}" for k in sorted(self.recall_at))
        return (f"  {title:26} n={self.n:3}  {ks}  MRR@10={self.mrr10:.3f}  "
                f"nDCG@10={self.ndcg10:.3f}  bay={self.trap_rate:.3f}  "
                f"truot={self.zero_hit}")


def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_one(
    ranked_chunks: list[Chunk],
    q: EvalQuery,
    ks: tuple[int, ...] = (1, 3, 5, 10),
) -> tuple[dict[int, float], float, float, bool, bool]:
    gold = set(q.relevant_chunk_ids)
    ids = [c.chunk_id for c in ranked_chunks]

    recall = {}
    for k in ks:
        hit = len(gold.intersection(ids[:k]))
        recall[k] = hit / len(gold) if gold else 0.0

    rr = 0.0
    for i, cid in enumerate(ids[:10], start=1):
        if cid in gold:
            rr = 1.0 / i
            break

    gains = [1.0 if cid in gold else 0.0 for cid in ids[:10]]
    ideal = [1.0] * min(len(gold), 10)
    ndcg = (_dcg(gains) / _dcg(ideal)) if ideal and _dcg(ideal) else 0.0

    tripped = any(c.status != "active" for c in ranked_chunks)
    missed = not gold.intersection(ids[:10])
    return recall, rr, ndcg, tripped, missed


def aggregate(rows: list[tuple[dict[int, float], float, float, bool, bool]]) -> Metrics:
    m = Metrics(n=len(rows))
    if not rows:
        return m
    ks = sorted(rows[0][0])
    for k in ks:
        m.recall_at[k] = sum(r[0][k] for r in rows) / len(rows)
    m.mrr10 = sum(r[1] for r in rows) / len(rows)
    m.ndcg10 = sum(r[2] for r in rows) / len(rows)
    m.trap_rate = sum(1 for r in rows if r[3]) / len(rows)
    m.zero_hit = sum(1 for r in rows if r[4])
    return m


def by_query_type(
    per_query: list[tuple[EvalQuery, tuple]],
) -> dict[str, Metrics]:
    buckets: dict[str, list[tuple]] = defaultdict(list)
    for q, row in per_query:
        buckets[q.query_type].append(row)
    return {t: aggregate(rows) for t, rows in sorted(buckets.items())}


def paraphrase_spread(per_query: list[tuple[EvalQuery, tuple]]) -> float:
    """Mean within-question spread of nDCG across paraphrases of the same question.

    Q01-V1/V2/V3 ask one thing three ways. A retriever that scores well on V1 and badly
    on V3 is brittle in exactly the way a production copilot cannot afford, and an
    average over all variants hides it. High spread is also a prefix-cache problem: three
    phrasings that retrieve three different chunk sets share no cacheable prompt prefix.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for q, row in per_query:
        groups[q.base_question_id].append(row[2])
    spreads = [max(v) - min(v) for v in groups.values() if len(v) > 1]
    return sum(spreads) / len(spreads) if spreads else 0.0
