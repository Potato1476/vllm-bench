"""Dense retrieval: the half of the hybrid that survives rewording.

WHY THIS EXISTS, measured rather than assumed. Lexical-only retrieval on the 144 gold
queries splits sharply by query type:

    retrieval   R@1=0.693  R@10=0.932   BM25 is already good
    hybrid      R@1=0.177  R@10=0.425
    reasoning   R@1=0.000  R@10=0.471   BM25 is blind

The reasoning queries are blind spots for a reason no amount of term weighting fixes.
"Số chuyến hoàn thành giảm dù booking tăng thì nên điều tra gì?" must retrieve
POLICY-ANALYSIS-001 and two playbooks, and shares almost no content word with any of
them: the question is phrased as a symptom, the documents are phrased as procedures.
Nothing in a bag of terms connects the two. That is the gap dense retrieval closes.

MEASURED, and the result is not "dense wins" (rag/ablation.py reproduces it):

                    R@10    MRR    nDCG   complete misses
    lexical+canon   0.616  0.734   0.570         4
    dense           0.644  0.750   0.606        20
    hybrid (RRF)    0.676  0.833   0.650         4

Dense is better on average and much worse in the tail: it returns nothing useful on 20 of
144 queries where lexical finds something, because an exact identifier -- fact_bookings,
METRIC-TRIP-001 -- is precisely what a 1024-dimensional average is bad at holding. The
fusion keeps dense's average and lexical's floor, which is the actual argument for hybrid
here, and it is an argument neither ranker's own score could have made.

By query type, R@1 / R@10:

                    lexical         dense          hybrid
    retrieval    0.773 / 0.955  0.864 / 1.000  0.864 / 1.000
    hybrid       0.271 / 0.463  0.225 / 0.458  0.263 / 0.504
    reasoning    0.000 / 0.483  0.117 / 0.600  0.183 / 0.650

The reasoning row is why this module exists. The hybrid row is why RRF does: both rankers
score ~0.46 alone and 0.504 fused, so the fusion is finding documents that neither ranker
put in its own top 10.

LATENCY. Encoding one query is p50 21 ms / p95 89 ms on Apple Silicon MPS, against 7 ms
per query when batching 40. The batched figure is what an offline job sees; the request
path sees the first one, and it lands on the TTFT budget before the engine starts. Worth
knowing before someone quotes the batch number in a capacity plan.

MODEL CHOICE. AITeamVN/Vietnamese_Embedding, a BGE-M3 fine-tune on ~300k Vietnamese
triplets. On the Legal Zalo 2021 benchmark, which is out of domain for it:

    Vietnamese_Embedding    Acc@1 0.727   MRR@10 0.818
    BKAI vietnamese-bi-encoder    0.711          0.795
    BGE-M3 (base, untuned)        0.568          0.682

The +16 points over its own base model is the whole argument for using a Vietnamese
fine-tune rather than a multilingual generalist. 1024 dimensions, 2048 max sequence,
apache-2.0, 0.6B parameters.

TWO BACKENDS, ONE INTERFACE, because where this runs matters more than how.

  SentenceTransformerBackend  loads the model in-process. Right for the offline corpus
                              encode, which happens once and writes a .npy; wrong for the
                              request path, where it would put 2.4 GB of weights and a
                              torch import inside the gateway.
  HttpBackend                 calls a text-embeddings-inference service. This is the
                              production shape: the model lives on the GPU node, in the
                              second time-slicing slot beside vLLM, and the gateway stays
                              a thin process that can be scaled and restarted freely.

DOCUMENT VECTORS ARE PRECOMPUTED, ALWAYS. 798 chunks encode once in minutes and then sit
in a 3.2 MB float32 array. Encoding them per query would add the corpus to every request's
latency budget for no benefit -- the corpus does not change between questions.

ASYMMETRY. BGE-M3 derivatives are trained with the query and the passage playing different
roles, so a query is encoded as a query and a passage as a passage. Encoding both the same
way is a quiet quality loss that shows up as a few points of recall and no error.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

MODEL_ID = "AITeamVN/Vietnamese_Embedding"
DIM = 1024
MAX_SEQ = 2048


class EmbeddingBackend(Protocol):
    def encode(self, texts: Sequence[str], *, is_query: bool) -> np.ndarray:
        """L2-normalised float32, shape (len(texts), DIM)."""


class SentenceTransformerBackend:
    """In-process. For the offline encode, and for tests that need real vectors."""

    def __init__(self, model_id: str = MODEL_ID, device: str | None = None,
                 batch_size: int = 16):
        from sentence_transformers import SentenceTransformer  # heavy, import late

        self.model = SentenceTransformer(model_id, device=device)
        self.model.max_seq_length = MAX_SEQ
        self.batch_size = batch_size

    def encode(self, texts: Sequence[str], *, is_query: bool) -> np.ndarray:
        vecs = self.model.encode(
            list(texts),
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,   # so cosine is a dot product
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


class HttpBackend:
    """Talks to text-embeddings-inference. The shape this takes in the cluster.

    TEI is what the model card lists as its serving path, it batches across concurrent
    callers, and it keeps torch out of this process entirely.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def encode(self, texts: Sequence[str], *, is_query: bool) -> np.ndarray:
        body = json.dumps({"inputs": list(texts), "normalize": True}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/embed", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return np.asarray(json.load(r), dtype=np.float32)


@dataclass
class DenseIndex:
    """Precomputed corpus vectors. Exact search -- 798 x 1024 does not need ANN.

    A brute-force dot product over this corpus is one 3.2 MB matrix multiply, roughly a
    millisecond. FAISS or HNSW would add a dependency, an index build step and an
    approximation error to save nothing measurable. Revisit past ~100k chunks.
    """

    ids: list[str]
    vectors: np.ndarray          # (n, DIM) float32, L2-normalised

    def __post_init__(self) -> None:
        if self.vectors.shape[0] != len(self.ids):
            raise ValueError("ids and vectors disagree on length")

    def search(self, qvec: np.ndarray, k: int) -> list[tuple[str, float]]:
        sims = self.vectors @ qvec.reshape(-1)
        k = min(k, len(self.ids))
        # argpartition then sort only the top slice: O(n) instead of O(n log n).
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(self.ids[i], float(sims[i])) for i in top]

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path.with_suffix(".npy"), self.vectors)
        path.with_suffix(".ids.json").write_text(
            json.dumps({"model": MODEL_ID, "dim": DIM, "ids": self.ids}),
            encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "DenseIndex":
        path = Path(path)
        meta = json.loads(path.with_suffix(".ids.json").read_text(encoding="utf-8"))
        if meta.get("model") != MODEL_ID:
            # Vectors from a different model are not comparable, and the failure is
            # silent: search still returns ten results, all of them wrong.
            raise ValueError(
                f"index built with {meta.get('model')}, code expects {MODEL_ID}")
        return cls(ids=meta["ids"], vectors=np.load(path.with_suffix(".npy")))


class DenseRankerAdapter:
    """Implements rag.retrieve.DenseRanker over an index plus a backend."""

    def __init__(self, index: DenseIndex, backend: EmbeddingBackend):
        self.index = index
        self.backend = backend

    def rank(self, query: str, k: int) -> list[tuple[str, float]]:
        qvec = self.backend.encode([query], is_query=True)[0]
        return self.index.search(qvec, k)


def build_index(chunk_ids: Sequence[str], texts: Sequence[str],
                backend: EmbeddingBackend) -> DenseIndex:
    return DenseIndex(ids=list(chunk_ids),
                      vectors=backend.encode(texts, is_query=False))
