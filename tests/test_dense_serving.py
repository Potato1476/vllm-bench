"""Dense retrieval on the serving path.

rag/ has had a dense ranker, a measured +0.08 nDCG@10 over lexical alone and -16 complete
misses since the retrieval work; services/llm_pipeline/app.py went on retrieving with BM25
only, because nothing constructed the ranker. These tests cover the wiring, and in
particular the two ways it is allowed to fail.

Enabling it in a cluster needs an embeddings service as well, which is why the failure
modes matter more than the happy path: a missing or slow embedder must cost the answer
some quality, never cost the caller the request.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from services.llm_pipeline import app

INDEX = Path("data/index/dense-vi")


class _FakeTei(BaseHTTPRequestHandler):
    """Stands in for text-embeddings-inference.

    Returns the stored vector of one known chunk rather than a random one, so that dense
    ranking has a deterministic right answer and the test asserts on retrieval rather than
    on whether the plumbing merely ran.
    """

    vector: list[float] = []
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        size = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(size))
        body = json.dumps([type(self).vector]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


@unittest.skipUnless(INDEX.with_suffix(".npy").exists(),
                     "dense index absent -- run `make dense-build`")
class DenseServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from rag import dense

        index = dense.DenseIndex.load(INDEX)
        cls.target = index.ids[0]
        _FakeTei.vector = [float(x) for x in index.vectors[0]]
        cls.tei = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTei)
        threading.Thread(target=cls.tei.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.tei.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tei.shutdown()
        cls.tei.server_close()

    def setUp(self) -> None:
        self.saved = (app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT)

    def tearDown(self) -> None:
        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = self.saved

    def test_both_halves_present_builds_a_ranker(self) -> None:
        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = str(INDEX), self.endpoint
        ranker = app._load_dense()
        self.assertIsNotNone(ranker)
        # The query embedding is whatever the fake returns, which is chunk 0's own vector,
        # so chunk 0 has to come back first with a similarity of 1.
        hits = ranker.rank("bất kỳ câu hỏi nào", 5)
        self.assertEqual(hits[0][0], self.target)
        self.assertAlmostEqual(hits[0][1], 1.0, places=4)

    def test_index_without_an_embedder_stays_lexical(self) -> None:
        # Half-configured is the state a deployment lands in when the embeddings service
        # has not been rolled out yet. It must read as "off", not as "broken".
        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = str(INDEX), ""
        self.assertIsNone(app._load_dense())

    def test_an_unreachable_embedder_does_not_stop_startup(self) -> None:
        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = str(INDEX), "http://127.0.0.1:1"
        # Construction succeeds: the backend is not contacted until a query arrives, and
        # refusing to start over an embedder that is merely slow to roll out would take
        # the whole platform down with it.
        self.assertIsNotNone(app._load_dense())

    def test_a_missing_index_is_refused_rather_than_served_empty(self) -> None:
        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = "data/index/does-not-exist", self.endpoint
        self.assertIsNone(app._load_dense())

    def test_hybrid_changes_what_retrieval_returns(self) -> None:
        """The point of the wiring: the ranker has to reach retrieval, not just exist."""
        from guardrails import pipeline
        from prompt.build import Session
        from rag import policy

        app.DENSE_INDEX_PATH, app.DENSE_ENDPOINT = str(INDEX), self.endpoint
        ranker = app._load_dense()
        question = "Một chuyến xe được tính là hoàn thành khi đáp ứng điều kiện nào?"
        session = Session(agent="t", access_level="internal-demo")

        before = _FakeTei.calls
        hybrid = pipeline.prepare(question, app._index, app._chunks_by_id, session,
                                  policy=policy.Policy(access_level="internal-demo"),
                                  dense=ranker)
        self.assertGreater(_FakeTei.calls, before, "retrieval never called the embedder")

        lexical = pipeline.prepare(question, app._index, app._chunks_by_id, session,
                                   policy=policy.Policy(access_level="internal-demo"))
        # The fake pins one chunk to the top of the dense ranking, so RRF must pull it
        # into the fused result. If the two agree exactly, `dense=` was ignored.
        self.assertTrue(hybrid.ok and lexical.ok)
        fused = [c.chunk_id for c in hybrid.context]
        self.assertIn(self.target, fused)
        self.assertNotEqual(fused, [c.chunk_id for c in lexical.context])


if __name__ == "__main__":
    unittest.main()
