"""Redis-backed semantic response cache with document-level invalidation.

The cache stores only answers that have already passed output grounding and PII checks.
Entries are partitioned by model, access level, policy version and corpus version.  Each
entry is also added to a reverse set for every source document, allowing a changed or
deprecated document to invalidate precisely the answers that may have depended on it.

Redis itself does not need a vector extension at this scale.  The lab has at most a few
hundred cached answers: Redis returns a bounded recent candidate set and numpy computes
cosine similarity locally.  This keeps the deployment on the small official Redis image
and makes the correctness threshold explicit and testable.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class RedisLike(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: Any, **kwargs: Any) -> Any: ...
    def mget(self, keys: Sequence[str]) -> list[Any]: ...
    def delete(self, *names: str) -> int: ...
    def sadd(self, name: str, *values: Any) -> int: ...
    def smembers(self, name: str) -> set[Any]: ...
    def expire(self, name: str, seconds: int) -> Any: ...
    def zadd(self, name: str, mapping: dict[str, float]) -> int: ...
    def zrevrange(self, name: str, start: int, end: int) -> list[Any]: ...
    def zrem(self, name: str, *values: Any) -> int: ...


class Embedder(Protocol):
    def encode(self, texts: Sequence[str], *, is_query: bool) -> Any: ...


@dataclass(frozen=True)
class CacheHit:
    text: str
    cited: tuple[str, ...]
    document_ids: tuple[str, ...]
    verdict: str
    kind: str
    similarity: float


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


class SemanticResponseCache:
    def __init__(
        self,
        client: RedisLike,
        *,
        embedder: Embedder | None = None,
        policy_version: str = "builtin-v1",
        corpus_version: str = "bundled",
        ttl_seconds: int = 3600,
        similarity_threshold: float = 0.96,
        max_candidates: int = 256,
        prefix: str = "guardrail-cache:v1",
    ) -> None:
        if not 0.0 < similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be in (0, 1]")
        self.client = client
        self.embedder = embedder
        self.policy_version = policy_version
        self.corpus_version = corpus_version
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates
        self.prefix = prefix
        self._local = threading.local()

    def _scope(self, model: str, access_level: str, variant: str = "") -> str:
        raw = "\x1f".join((model, access_level, self.policy_version, self.corpus_version, variant))
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _base(self, model: str, access_level: str, variant: str = "") -> str:
        return f"{self.prefix}:{self._scope(model, access_level, variant)}"

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def _vector(self, query: str) -> list[float] | None:
        if self.embedder is None:
            return None
        if getattr(self._local, "query", None) == query:
            return getattr(self._local, "vector", None)
        try:
            vec = self.embedder.encode([query], is_query=True)[0]
            vector = [float(value) for value in vec]
            # lookup() and store() run in the same request thread. Reusing this vector
            # avoids sending the same question to TEI twice on every cache miss.
            self._local.query = query
            self._local.vector = vector
            return vector
        except Exception:  # the cache and embedder may never take serving down
            return None

    @staticmethod
    def _similarity(left: list[float], right: list[float]) -> float:
        # Embedding backends promise normalised vectors, but normalise again so a serving
        # configuration mistake cannot turn a dot product greater than one into a hit.
        import numpy as np

        a = np.asarray(left, dtype=np.float32)
        b = np.asarray(right, dtype=np.float32)
        if a.shape != b.shape or a.ndim != 1:
            return -1.0
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(a @ b / denom) if denom else -1.0

    def lookup(
        self, query: str, *, model: str, access_level: str, variant: str = ""
    ) -> CacheHit | None:
        base = self._base(model, access_level, variant)
        exact_key = f"{base}:exact:{self._digest(query)}"
        entry_key = _text(self.client.get(exact_key))
        if entry_key:
            entry = self._load(entry_key)
            if entry is not None:
                return self._hit(entry, "exact", 1.0)
            self.client.delete(exact_key)

        vector = self._vector(query)
        if vector is None:
            return None
        keys = [
            _text(k) for k in self.client.zrevrange(f"{base}:entries", 0, self.max_candidates - 1)
        ]
        valid_keys = [key for key in keys if key]
        if not valid_keys:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        stale: list[str] = []
        for key, raw in zip(valid_keys, self.client.mget(valid_keys), strict=True):
            if raw is None:
                stale.append(key)
                continue
            try:
                entry = json.loads(_text(raw) or "")
                score = self._similarity(vector, entry["vector"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                stale.append(key)
                continue
            if best is None or score > best[0]:
                best = (score, entry)
        if stale:
            self.client.zrem(f"{base}:entries", *stale)
        if best is None or best[0] < self.similarity_threshold:
            return None
        return self._hit(best[1], "semantic", best[0])

    def _load(self, key: str) -> dict[str, Any] | None:
        raw = _text(self.client.get(key))
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _hit(entry: dict[str, Any], kind: str, similarity: float) -> CacheHit:
        return CacheHit(
            text=str(entry["text"]),
            cited=tuple(str(v) for v in entry.get("cited", [])),
            document_ids=tuple(str(v) for v in entry.get("document_ids", [])),
            verdict=str(entry.get("verdict", "ok")),
            kind=kind,
            similarity=similarity,
        )

    def store(
        self,
        query: str,
        *,
        model: str,
        access_level: str,
        text: str,
        cited: Sequence[str],
        document_ids: Sequence[str],
        verdict: str = "ok",
        variant: str = "",
    ) -> None:
        base = self._base(model, access_level, variant)
        digest = self._digest(query)
        exact_key = f"{base}:exact:{digest}"
        entry_key = f"{base}:entry:{uuid.uuid4().hex}"
        vector = self._vector(query)
        entry = {
            "text": text,
            "cited": list(cited),
            # Track every admitted context document, not only citations.  The model may
            # derive wording from an uncited passage, so citation-only invalidation leaks
            # stale answers after that passage changes.
            "document_ids": sorted(set(document_ids)),
            "verdict": verdict,
            "vector": vector,
            "exact_key": exact_key,
            "entry_key": entry_key,
            "entries_key": f"{base}:entries",
        }
        self.client.set(entry_key, json.dumps(entry, ensure_ascii=False), ex=self.ttl_seconds)
        self.client.set(exact_key, entry_key, ex=self.ttl_seconds)
        if vector is not None:
            self.client.zadd(f"{base}:entries", {entry_key: time.time()})
            self.client.expire(f"{base}:entries", self.ttl_seconds)
        for document_id in entry["document_ids"]:
            reverse = f"{self.prefix}:doc:{self._digest(document_id)}"
            self.client.sadd(reverse, entry_key)
            self.client.expire(reverse, self.ttl_seconds)

    def invalidate_document(self, document_id: str) -> int:
        reverse = f"{self.prefix}:doc:{self._digest(document_id)}"
        keys = [_text(key) for key in self.client.smembers(reverse)]
        removed = 0
        for key in (key for key in keys if key):
            entry = self._load(key)
            if entry is not None:
                exact_key = str(entry.get("exact_key", ""))
                # A newer answer for the same exact question may already point elsewhere.
                # Invalidating the old document must not erase that newer pointer.
                if exact_key and _text(self.client.get(exact_key)) == key:
                    self.client.delete(exact_key)
                entries_key = str(entry.get("entries_key", ""))
                if entries_key:
                    self.client.zrem(entries_key, key)
            removed += self.client.delete(key)
        self.client.delete(reverse)
        return removed

    def clear(self) -> int:
        # redis-py scan_iter is intentionally used only by the administrative path.
        scan_iter = getattr(self.client, "scan_iter", None)
        if scan_iter is None:
            raise RuntimeError("Redis client does not support scan_iter")
        keys = list(scan_iter(match=f"{self.prefix}:*", count=500))
        return self.client.delete(*keys) if keys else 0
