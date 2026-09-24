from __future__ import annotations

import fnmatch
import unittest

import numpy as np

from services.llm_pipeline.semantic_cache import SemanticResponseCache


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def get(self, name: str):
        return self.values.get(name)

    def set(self, name: str, value, **_kwargs):
        self.values[name] = value
        return True

    def mget(self, keys):
        return [self.values.get(key) for key in keys]

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            for collection in (self.values, self.sets, self.zsets):
                if name in collection:
                    del collection[name]
                    removed += 1
                    break
        return removed

    def sadd(self, name: str, *values) -> int:
        target = self.sets.setdefault(name, set())
        before = len(target)
        target.update(str(value) for value in values)
        return len(target) - before

    def smembers(self, name: str):
        return set(self.sets.get(name, set()))

    def expire(self, _name: str, _seconds: int):
        return True

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        target = self.zsets.setdefault(name, {})
        before = len(target)
        target.update(mapping)
        return len(target) - before

    def zrevrange(self, name: str, start: int, end: int):
        ordered = sorted(self.zsets.get(name, {}).items(), key=lambda item: -item[1])
        return [key for key, _ in ordered[start : end + 1]]

    def zrem(self, name: str, *values) -> int:
        target = self.zsets.setdefault(name, {})
        removed = 0
        for value in values:
            removed += int(target.pop(str(value), None) is not None)
        return removed

    def scan_iter(self, match: str, count: int = 10):  # noqa: ARG002
        keys = set(self.values) | set(self.sets) | set(self.zsets)
        yield from (key for key in keys if fnmatch.fnmatch(key, match))


class FakeEmbedder:
    vectors = {
        "doanh thu hôm nay": [1.0, 0.0, 0.0],
        "doanh thu trong ngày": [0.99, 0.05, 0.0],
        "tỷ lệ huỷ": [0.0, 1.0, 0.0],
    }

    def encode(self, texts, *, is_query: bool):  # noqa: ARG002
        return np.asarray([self.vectors[text] for text in texts], dtype=np.float32)


class SemanticCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = MemoryRedis()
        self.cache = SemanticResponseCache(
            self.redis, embedder=FakeEmbedder(), similarity_threshold=0.95
        )

    def store(self, query: str = "doanh thu hôm nay") -> None:
        self.cache.store(
            query,
            model="qwen2.5-7b",
            access_level="internal-demo",
            text="Doanh thu là 10 [KPI-001].",
            cited=["KPI-001"],
            document_ids=["KPI-001", "SCHEMA-001"],
        )

    def test_exact_hit_does_not_need_an_embedding_call(self) -> None:
        exact = SemanticResponseCache(self.redis, embedder=None)
        exact.store(
            "doanh thu hôm nay",
            model="qwen2.5-7b",
            access_level="internal-demo",
            text="an toàn",
            cited=["KPI-001"],
            document_ids=["KPI-001"],
        )
        hit = exact.lookup("doanh thu hôm nay", model="qwen2.5-7b", access_level="internal-demo")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "exact")

    def test_close_paraphrase_is_a_semantic_hit(self) -> None:
        self.store()
        hit = self.cache.lookup(
            "doanh thu trong ngày", model="qwen2.5-7b", access_level="internal-demo"
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "semantic")
        self.assertGreater(hit.similarity, 0.95)

    def test_unrelated_question_misses(self) -> None:
        self.store()
        self.assertIsNone(
            self.cache.lookup("tỷ lệ huỷ", model="qwen2.5-7b", access_level="internal-demo")
        )

    def test_model_and_access_level_are_hard_partitions(self) -> None:
        self.store()
        self.assertIsNone(
            self.cache.lookup(
                "doanh thu hôm nay", model="qwen2.5-1.5b", access_level="internal-demo"
            )
        )
        self.assertIsNone(
            self.cache.lookup("doanh thu hôm nay", model="qwen2.5-7b", access_level="public")
        )

    def test_generation_variant_is_a_hard_partition(self) -> None:
        self.cache.store(
            "doanh thu hôm nay",
            model="qwen2.5-7b",
            access_level="internal-demo",
            variant="max-tokens-20",
            text="ngắn",
            cited=["KPI-001"],
            document_ids=["KPI-001"],
        )
        self.assertIsNone(
            self.cache.lookup(
                "doanh thu hôm nay",
                model="qwen2.5-7b",
                access_level="internal-demo",
                variant="max-tokens-200",
            )
        )

    def test_document_invalidation_removes_every_dependent_answer(self) -> None:
        self.store()
        self.assertEqual(self.cache.invalidate_document("SCHEMA-001"), 1)
        self.assertIsNone(
            self.cache.lookup("doanh thu hôm nay", model="qwen2.5-7b", access_level="internal-demo")
        )

    def test_corpus_version_changes_the_namespace(self) -> None:
        self.store()
        changed = SemanticResponseCache(
            self.redis, embedder=FakeEmbedder(), corpus_version="bundled-v2"
        )
        self.assertIsNone(
            changed.lookup("doanh thu hôm nay", model="qwen2.5-7b", access_level="internal-demo")
        )


if __name__ == "__main__":
    unittest.main()
