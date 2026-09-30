#!/usr/bin/env python3
"""Can a classifier on the question text decide which model should answer it?

WHY THIS RUNS BEFORE ANY ROUTER IS BUILT

The platform's cost story is that a small model handles simple work. Measured, the 1.5B
cannot do the RAG answering it is currently assigned: 3 of 44 single-fact questions pass
grounding unaided. The proposal that survives that is to give the small model a different
job -- deciding which model should answer -- and route by question type.

That proposal rests on one unverified assumption: that question TEXT predicts difficulty.
Hand-crafted features measured earlier say the opposite. Input token count does not
separate the classes at all (the three distributions overlap almost exactly, because 96%
of the prompt is retrieved documents), and the best retrieval-side feature reached 72%
against a 69% majority baseline -- inside the noise.

So this is the cheap test that comes first: 0 USD, no GPU, no cluster. If a learned
classifier cannot beat the majority baseline either, the whole routing branch is dead and
a week is saved. If it can, there is a number to take to the mentor instead of an idea.

THE TRAP THIS DATASET SETS

The 144 gold queries are 36 BASE QUESTIONS with 4 paraphrases each, and every paraphrase
carries its base question's label. Split those 144 rows at random and variants of the
same question land in both train and test: the model memorises the question, scores
beautifully, and has learned nothing transferable. The effective sample size is 36, not
144, and no amount of paraphrasing changes that.

This script reports both splits on purpose. The leaky one is not a mistake to be hidden;
it is the number somebody will produce by accident, and seeing the gap is the argument
for not trusting it.

    make route-classifier
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "xanhsm_retrieval_mock" / "eval" / "retrieval_eval.jsonl"

# "simple" means: a single-fact lookup the small model might survive. The measured
# content overlap for qwen2.5-1.5b is 66% on this class and 20%/15% on the other two, so
# this is where the boundary sits if it sits anywhere.
SIMPLE = {"retrieval"}


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    words = re.findall(r"[^\W_]+", text, re.UNICODE)
    # Unigrams plus bigrams: Vietnamese carries much of its meaning in word pairs
    # ("bao nhiêu", "như thế nào"), and question form is exactly what should signal type.
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]


def tfidf(train_texts: list[str], test_texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Vocabulary and IDF come from TRAIN only. Fitting on everything is leakage too."""
    docs = [tokens(t) for t in train_texts]
    df = Counter()
    for d in docs:
        df.update(set(d))
    vocab = {w: i for i, w in enumerate(sorted(w for w, c in df.items() if c >= 2))}
    if not vocab:
        vocab = {w: i for i, w in enumerate(sorted(df))}
    n = len(docs)
    idf = np.zeros(len(vocab))
    for w, i in vocab.items():
        idf[i] = math.log((1 + n) / (1 + df[w])) + 1.0

    def vectorise(texts: list[str]) -> np.ndarray:
        m = np.zeros((len(texts), len(vocab)))
        for r, t in enumerate(texts):
            counts = Counter(tokens(t))
            for w, c in counts.items():
                if w in vocab:
                    m[r, vocab[w]] = c
        m *= idf
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return m / norms

    return vectorise(train_texts), vectorise(test_texts)


def fit_logreg(x: np.ndarray, y: np.ndarray, l2: float = 1.0,
               steps: int = 600, lr: float = 0.5) -> np.ndarray:
    """Plain L2 logistic regression. Deliberately the simplest thing that could work:
    with 36 independent examples, anything with capacity will memorise them."""
    w = np.zeros(x.shape[1] + 1)
    xb = np.hstack([x, np.ones((x.shape[0], 1))])
    for _ in range(steps):
        p = 1.0 / (1.0 + np.exp(-xb @ w))
        grad = xb.T @ (p - y) / len(y)
        grad[:-1] += l2 * w[:-1] / len(y)
        w -= lr * grad
    return w


def predict(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    xb = np.hstack([x, np.ones((x.shape[0], 1))])
    return (1.0 / (1.0 + np.exp(-xb @ w)) >= 0.5).astype(int)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def main() -> int:
    rows = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = [r["query"] for r in rows]
    y = np.array([1 if r["query_type"] in SIMPLE else 0 for r in rows])
    groups = [r["base_question_id"] for r in rows]
    uniq = sorted(set(groups))

    print(f"  {len(rows)} dòng = {len(uniq)} câu gốc x {len(rows)//len(uniq)} biến thể")
    print(f"  nhãn: {int(y.sum())} 'đơn giản' / {len(y) - int(y.sum())} 'phức tạp'")
    base = max(y.mean(), 1 - y.mean())
    print(f"  baseline (đoán nhãn đa số): {base:.1%}\n")

    # --- honest: leave one BASE QUESTION out ------------------------------------------
    correct = np.zeros(len(rows), dtype=bool)
    for g in uniq:
        te = [i for i, x in enumerate(groups) if x == g]
        tr = [i for i, x in enumerate(groups) if x != g]
        xtr, xte = tfidf([texts[i] for i in tr], [texts[i] for i in te])
        w = fit_logreg(xtr, y[tr])
        correct[te] = predict(w, xte) == y[te]
    acc = correct.mean()
    # The interval is over the 36 INDEPENDENT questions, not the 144 correlated rows.
    per_group = np.array([correct[[i for i, x in enumerate(groups) if x == g]].mean()
                          for g in uniq])
    lo, hi = wilson(int(round(per_group.sum())), len(uniq))
    print(f"  CHIA THEO CÂU GỐC (đúng phương pháp)")
    print(f"    accuracy        {acc:.1%}   ({int(correct.sum())}/{len(rows)} dòng)")
    print(f"    khoảng tin 95%  [{lo:.1%}, {hi:.1%}]   (trên {len(uniq)} câu gốc độc lập)")
    print(f"    so với baseline {acc - base:+.1%}")

    # --- leaky: random rows, for comparison -------------------------------------------
    rng = np.random.default_rng(0)
    leaky = []
    for _ in range(20):
        idx = rng.permutation(len(rows))
        cut = int(len(rows) * 0.75)
        tr, te = idx[:cut], idx[cut:]
        xtr, xte = tfidf([texts[i] for i in tr], [texts[i] for i in te])
        w = fit_logreg(xtr, y[tr])
        leaky.append((predict(w, xte) == y[te]).mean())
    print(f"\n  CHIA NGẪU NHIÊN THEO DÒNG (rò rỉ -- KHÔNG dùng được)")
    print(f"    accuracy        {np.mean(leaky):.1%}")
    print(f"    thổi phồng      {np.mean(leaky) - acc:+.1%} so với cách chia đúng")

    print()
    if lo > base:
        print("  => Tín hiệu THẬT: cận dưới vượt baseline. Đáng mang đi bàn tiếp.")
        return 0
    print("  => KHÔNG chứng minh được tín hiệu: cận dưới không vượt baseline.")
    print(f"     Với {len(uniq)} câu gốc, một classifier trên văn bản câu hỏi không tách")
    print("     được 'đơn giản' khỏi 'phức tạp' ở mức tin cậy được. Nhánh router theo")
    print("     độ phức tạp cần dữ liệu thật trước, không phải code trước.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
