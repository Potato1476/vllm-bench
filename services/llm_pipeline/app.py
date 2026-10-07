"""OpenAI-compatible HTTP adapter around ``guardrails.pipeline``.

LiteLLM sends requests here instead of directly to vLLM.  The adapter prepares the
retrieval-augmented prompt, calls the selected vLLM service, then validates the complete
answer before releasing any bytes to the caller.  Streaming requests are deliberately
buffered upstream and emitted as SSE only after the output guardrails pass; true token
streaming would make it impossible to retract PII or a fabricated citation.

Only the stdlib is used so the serving image contains the exact same guardrail modules
tested by ``make guardrails-test`` without pulling a second web framework dependency.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from guardrails import pipeline, spotlight
from prompt.build import Session
from rag import bm25, policy
from rag.corpus import DEFAULT_CORPUS, load_chunks
from services.llm_pipeline import tracing
from services.llm_pipeline.semantic_cache import CacheHit, SemanticResponseCache

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "300"))
DEFAULT_ACCESS_LEVEL = os.getenv("DEFAULT_ACCESS_LEVEL", "internal-demo")
# Applied only when the caller named no limit of its own. A request without max_tokens
# generates until the model decides to stop, which on a 3s p95 objective is a promise the
# platform cannot keep: decode speed is fixed, so length is the only term it controls.
#
# 192 is deliberately generous against the measured need -- the 144 reference answers for
# this corpus are 32 tokens at the median and 54 at the longest -- because this is a
# backstop against a runaway generation, not the mechanism for keeping answers short.
# That job belongs to the brevity rule in prompt/build.py, which shapes the answer rather
# than truncating it. A cap set near the expected length would cut correct answers off
# mid-citation, and a truncated citation is a grounding failure.
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "192"))

# EVERY SERVED ANSWER SAYS THE CORPUS IS SYNTHETIC, AND THE PROMPT IS NOT ALLOWED TO.
#
# data/xanhsm_retrieval_mock/manifest.json states it outright -- "No synthetic policy or
# figure in this dataset represents actual Xanh SM internal data" -- and 720 of its 798
# records are generated daily operations figures (seed=20260921) for real GreenSM cities.
#
# The grounding stage cannot catch that, and this is not a gap in it. grounding.py asks
# whether an answer is supported by the retrieved document; it has no notion of whether
# the document is true. So a question about GBV in Da Nang comes back with a figure, cited
# to a document that really is in the corpus, passing every check -- and it is fiction.
# A well-grounded answer over a synthetic corpus is indistinguishable from one over a real
# corpus, because the citation is the thing that manufactures the trust. That makes a
# confident fake strictly worse than no answer: an analyst who carries the number into a
# real report has been actively misled by the platform.
#
# Which is why this is not a line in the system prompt. Generation is stochastic -- the
# model summarises, and will sometimes keep the figure while dropping the "giả lập" that
# the source text carries. A measure whose job is to protect a colleague's real report
# cannot be sampled. It lives here, past the guardrails, where it is deterministic.
#
# Prepended rather than appended: a long answer gets read halfway, copied in part, or
# truncated by a client, and the tail is the part that disappears. Prepending also puts it
# first on the wire for a streamed response, so it is on screen while the answer arrives.
#
# ANSWER_NOTICE="" would serve answers bare. NOTHING IN THIS PROJECT SHOULD EVER SET IT.
# The team has no access to the company's internal documents, so the indexed corpus is
# simulated permanently rather than until some later milestone -- there is no second
# phase in which this notice becomes a lie. The switch exists because a deployment that
# DID index real documents would need it, not because this one is heading there.
DEFAULT_ANSWER_NOTICE = (
    "⚠️ Môi trường thử nghiệm: kho tài liệu là dữ liệu mô phỏng, không nối tới kho dữ "
    "liệu vận hành. Số liệu trong câu trả lời này không phải số liệu thật."
)
ANSWER_NOTICE = os.getenv("ANSWER_NOTICE", DEFAULT_ANSWER_NOTICE).strip()

# Models that cannot be trusted to cite unaided, and must have the format constrained at
# decode time. See _force_citation for the measurement behind this default. Set to an
# empty string to turn the constraint off entirely.
FORCE_CITATION_MODELS = frozenset(
    m.strip() for m in os.getenv("FORCE_CITATION_MODELS", "qwen2.5-1.5b").split(",")
    if m.strip()
)
CORPUS_PATH = os.getenv("CORPUS_PATH", str(DEFAULT_CORPUS))
MODEL_ROUTES = json.loads(os.getenv(
    "MODEL_ROUTES_JSON",
    json.dumps({
        "qwen2.5-7b": "http://vllm-a.inference.svc.cluster.local:8000/v1",
        "qwen2.5-1.5b": "http://vllm-b.inference.svc.cluster.local:8000/v1",
    }),
))

# THE NON-RAG SERVING PROFILE, AND WHY IT RIDES ON THE MODEL NAME.
#
# The platform is infrastructure for seven other projects (DA#19 ... DA#45), and only the
# MOC copilot among them is a cited-question-answering workload. finalise() blocks any
# answer that cites nothing, so a project doing classification or extraction would be
# refused at grounding on essentially every request -- it is not that the platform is
# unhelpful to them, it is unusable.
#
# What those teams need is PII screening, injection screening and instrumented serving.
# Those three do not depend on having retrieved a document; only the citation requirement
# does. So a second profile serves them with everything except retrieval and grounding.
#
# THE PROFILE MUST NOT COME FROM THE REQUEST. A header or body field naming the profile
# would let any caller turn grounding off for the MOC copilot too, which is exactly the
# privilege escalation do_POST already refuses for access_level. It rides on the MODEL
# NAME instead, because LiteLLM already enforces which models a virtual key may use --
# verified on this deployment, a restricted key gets
# `This key can only access models=['qwen2.5-1.5b']`. That makes the existing, tested
# authorisation boundary the profile boundary too, with no new mechanism to get wrong.
#
# So `qwen2.5-7b` is grounded and `qwen2.5-7b-plain` is not, and which of the two a
# project may call is decided when its key is minted.
PLAIN_PROFILE_SUFFIX = os.getenv("PLAIN_PROFILE_SUFFIX", "-plain")


def _resolve_profile(model: str) -> tuple[str, bool]:
    """Map a requested model to (engine model name, grounded).

    The engine only knows the base names, so the suffix is stripped before the upstream
    call -- vLLM would reject `qwen2.5-7b-plain` as an unknown model.
    """
    if (PLAIN_PROFILE_SUFFIX and model.endswith(PLAIN_PROFILE_SUFFIX)
            and model[: -len(PLAIN_PROFILE_SUFFIX)] in MODEL_ROUTES):
        return model[: -len(PLAIN_PROFILE_SUFFIX)], False
    return model, True


def _routable(model: str) -> bool:
    return _resolve_profile(model)[0] in MODEL_ROUTES


# Dense retrieval, off unless both halves are present.
#
# Measured on the 144 gold queries: lexical alone scores 0.570 nDCG@10, dense 0.606, the
# two fused by RRF 0.650. Lexical misses 4 queries completely, dense misses 20; fusion
# keeps lexical's exact-identifier floor while improving paraphrased and reasoning queries.
#
# Two things have to be true to turn it on, and they fail independently:
#   DENSE_INDEX_PATH   the precomputed corpus vectors, built offline by `make dense-build`
#                      on a machine with torch. Baked into the image or mounted.
#   DENSE_ENDPOINT     a text-embeddings-inference service, because the QUERY has to be
#                      embedded per request and this image has no model in it. See
#                      k8s/embeddings/tei.yaml.
#
# Absent either, retrieval stays lexical and the service starts normally. A missing
# embedding service must degrade the answer, never refuse the request.
DENSE_INDEX_PATH = os.getenv("DENSE_INDEX_PATH", "").strip()
DENSE_ENDPOINT = os.getenv("DENSE_ENDPOINT", "").strip()
DENSE_TIMEOUT = float(os.getenv("DENSE_TIMEOUT_SECONDS", "2.0"))

_tracer = tracing.from_environment()

_chunks = load_chunks(CORPUS_PATH)
_chunks_by_id = {chunk.chunk_id: chunk for chunk in _chunks}
_index = bm25.build([(chunk.chunk_id, chunk.text) for chunk in _chunks])


class _ResilientDense:
    """Makes "an embedder that is down costs quality, not the request" actually true.

    The startup path already degraded correctly: no index or no endpoint and the service
    comes up lexical. The REQUEST path did not, and the difference was invisible until an
    embeddings pod crash-looped under load. urllib raises URLError, it propagates out of
    pipeline.prepare, and the handler that catches URLError in do_POST is the one written
    for a failing vLLM -- so every request returned HTTP 504 "vLLM request failed:
    Connection refused" while vLLM was healthy and answering in 20ms.

    Two failures there, and the misattribution was the worse one: it pointed diagnosis at
    the GPU, which is the expensive thing to go looking at.

    rag.retrieve.hybrid fuses the rankings it is given with RRF, and an empty ranking
    contributes nothing to the fusion -- so returning [] here yields exactly the lexical
    order the service would have produced with dense switched off. The degradation is
    silent by construction, which is why it is counted.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def rank(self, query: str, k: int) -> list[tuple[str, float]]:
        try:
            return self.inner.rank(query, k)
        except Exception as exc:  # noqa: BLE001 -- any embedder fault degrades, none fails
            _telemetry.inc("guardrail_dense_failures_total",
                           {"reason": type(exc).__name__})
            return []


def _load_dense() -> Any:
    """Build the dense ranker, or return None and say why on stdout.

    Imported inside the function on purpose: rag.dense needs numpy, and the offline
    harnesses and this service's lexical-only deployment must both keep working in an
    image that does not carry it.
    """
    if not (DENSE_INDEX_PATH and DENSE_ENDPOINT):
        missing = [n for n, v in (("DENSE_INDEX_PATH", DENSE_INDEX_PATH),
                                  ("DENSE_ENDPOINT", DENSE_ENDPOINT)) if not v]
        print(f"dense retrieval off ({', '.join(missing)} unset); lexical only", flush=True)
        return None
    try:
        from rag import dense as dense_mod

        index = dense_mod.DenseIndex.load(DENSE_INDEX_PATH)
        # DenseIndex.load refuses vectors built by a different model. Letting that through
        # would not raise anything later -- search would return ten confident, wrong
        # neighbours -- so the check belongs at startup, where it is visible.
        ranker = _ResilientDense(dense_mod.DenseRankerAdapter(
            index, dense_mod.HttpBackend(DENSE_ENDPOINT, timeout=DENSE_TIMEOUT)
        ))
        print(f"dense retrieval on: {len(index.ids)} vectors, endpoint {DENSE_ENDPOINT}",
              flush=True)
        return ranker
    except Exception as exc:  # noqa: BLE001 -- startup must not depend on this
        print(f"dense retrieval unavailable ({type(exc).__name__}: {exc}); "
              f"falling back to lexical", flush=True)
        return None


_dense = _load_dense()
_semantic_cache_configured = os.getenv(
    "SEMANTIC_CACHE_ENABLED", "false"
).lower() in ("1", "true", "yes")


def _load_semantic_cache() -> SemanticResponseCache | None:
    """Connect to shared Redis or the local sidecar; failures leave serving uncached."""
    if not _semantic_cache_configured:
        return None
    try:
        import redis

        url = os.getenv("REDIS_URL", "").strip()
        socket = os.getenv("REDIS_UNIX_SOCKET", "/run/redis/redis.sock")
        if url:
            client = redis.Redis.from_url(url, socket_timeout=0.2,
                                          socket_connect_timeout=0.2)
        else:
            client = redis.Redis(unix_socket_path=socket, socket_timeout=0.2,
                                 socket_connect_timeout=0.2)
        client.ping()
        embedder = None
        cache_embedding_endpoint = os.getenv(
            "SEMANTIC_CACHE_EMBEDDING_ENDPOINT", DENSE_ENDPOINT
        ).strip()
        if cache_embedding_endpoint:
            from rag.dense import HttpBackend

            embedder = HttpBackend(
                cache_embedding_endpoint,
                timeout=float(os.getenv("SEMANTIC_CACHE_EMBEDDING_TIMEOUT_SECONDS", "0.3")),
            )
        cache = SemanticResponseCache(
            client,
            embedder=embedder,
            policy_version=os.getenv("POLICY_VERSION", "builtin-v1"),
            corpus_version=os.getenv("CORPUS_VERSION", "bundled"),
            ttl_seconds=int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "3600")),
            similarity_threshold=float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.96")),
            max_candidates=int(os.getenv("SEMANTIC_CACHE_MAX_CANDIDATES", "256")),
        )
        # Never print REDIS_URL: it may contain a password.
        print(f"semantic cache on: {'external Redis' if url else 'redis unix socket ' + socket}",
              flush=True)
        return cache
    except Exception as exc:  # cache availability must not become serving availability
        print(f"semantic cache unavailable ({type(exc).__name__}: {exc}); disabled", flush=True)
        return None


_semantic_cache = _load_semantic_cache()
_semantic_cache_retry_after = 0.0
_semantic_cache_lock = threading.Lock()


def _get_semantic_cache() -> SemanticResponseCache | None:
    """Reconnect after a Redis-sidecar startup race or restart."""
    global _semantic_cache, _semantic_cache_retry_after
    if _semantic_cache is not None or not _semantic_cache_configured:
        return _semantic_cache
    now = time.monotonic()
    if now < _semantic_cache_retry_after:
        return None
    with _semantic_cache_lock:
        if _semantic_cache is None and time.monotonic() >= _semantic_cache_retry_after:
            _semantic_cache = _load_semantic_cache()
            if _semantic_cache is None:
                _semantic_cache_retry_after = time.monotonic() + 5.0
    return _semantic_cache


_sessions: dict[tuple[str, str], Session] = {}
_sessions_lock = threading.Lock()


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    """Small dependency-free Prometheus registry for the guardrail service."""

    COUNTERS = {
        "guardrail_requests_total": (
            "Guardrail requests by outcome, terminal stage and routed model.",
            ("outcome", "stage", "model"),
        ),
        "guardrail_upstream_requests_total": (
            "Calls from the guardrail service to vLLM.",
            ("model", "outcome"),
        ),
        "guardrail_pii_findings_total": (
            "PII findings by direction, kind and action; values are never exported.",
            ("direction", "kind", "action"),
        ),
        "guardrail_injection_detections_total": (
            "Prompt-injection detections by source and action.",
            ("source", "action"),
        ),
        # Declared here, and the declaration is the whole point: inc() does
        # `self.COUNTERS[name]`, so an undeclared name raises KeyError inside request
        # handling and the caller gets HTTP 500. Adding the inc() call without this block
        # turned every grounding retry into a crash -- two 500s in 1201 requests, which
        # read as a platform fault and were mine.
        "guardrail_grounding_retry_total": (
            "Answers blocked at grounding that were given a second generation.",
            ("model",),
        ),
        "guardrail_grounding_retry_rescued_total": (
            "Second generations that passed grounding. The ratio to the line above is "
            "what says whether retrying is worth the GPU time.",
            ("model",),
        ),
        "guardrail_dense_failures_total": (
            "Dense-retrieval calls that failed and silently degraded to lexical.",
            ("reason",),
        ),
        "guardrail_documents_dropped_total": (
            "Retrieved documents excluded from the prompt by reason.",
            ("reason",),
        ),
        "guardrail_grounding_verdicts_total": (
            "Grounding checks by verdict.",
            ("verdict",),
        ),
        "guardrail_answers_declined_total": (
            "Answers that declined instead of asserting -- the corpus did not cover the "
            "question. Counted as allowed everywhere else, so this is the only signal "
            "that a question went unanswered. Carries no question text. Excludes cache "
            "hits, which return before the grounding report is read: divide by "
            "guardrail_upstream_requests_total{outcome=\"success\"}, not by all requests.",
            ("model",),
        ),
        "guardrail_citations_total": (
            "Citation observations by validity.",
            ("kind",),
        ),
        "guardrail_semantic_cache_requests_total": (
            "Response-cache lookups by result.",
            ("result",),
        ),
    }
    HISTOGRAMS = {
        "guardrail_request_duration_seconds": (
            "End-to-end time spent in the guardrail hop, including vLLM.",
            ("outcome", "model"),
            (0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 1.5, 3.0, 5.0, 10.0, 30.0, 120.0, 300.0),
        ),
        "guardrail_processing_duration_seconds": (
            "Guardrail processing time excluding the vLLM upstream call.",
            ("outcome", "model"),
            (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 3.0),
        ),
        "guardrail_stage_duration_seconds": (
            "Execution time of each guardrail pipeline stage.",
            ("stage",),
            (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0),
        ),
        "guardrail_upstream_duration_seconds": (
            "Time waiting for the routed vLLM upstream.",
            ("model",),
            (0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 3.0, 5.0, 10.0, 30.0, 120.0, 300.0),
        ),
        "guardrail_documents_retrieved": (
            "Number of documents admitted to the prompt per request.",
            ("model",),
            (0.0, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 50.0),
        ),
        "guardrail_grounding_overlap_ratio": (
            "Lexical overlap between an answer and its cited evidence.",
            ("verdict",),
            (0.0, 0.1, 0.2, 0.35, 0.5, 0.75, 0.9, 1.0),
        ),
    }

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counters: Counter[tuple[str, tuple[str, ...]]] = Counter()
        self.gauges: dict[tuple[str, tuple[str, ...]], float] = {}
        self.histograms: dict[
            tuple[str, tuple[str, ...]], tuple[list[int], float, int]
        ] = {}
        self.gauge_meta = {
            "guardrail_in_flight_requests": (
                "Completion requests currently executing in this process.", ()
            ),
            "guardrail_build_info": (
                "Build, policy and corpus identity for benchmark lineage.",
                ("version", "policy_version", "corpus_version"),
            ),
        }
        self.set_gauge("guardrail_in_flight_requests", {}, 0)
        self.set_gauge("guardrail_build_info", {
            "version": os.getenv("GUARDRAIL_VERSION", "dev"),
            "policy_version": os.getenv("POLICY_VERSION", "builtin-v1"),
            "corpus_version": os.getenv("CORPUS_VERSION", "bundled"),
        }, 1)
        self._seed_zero_series()

    @staticmethod
    def _label_values(meta: tuple[str, tuple[str, ...]], labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(str(labels[name]) for name in meta[1])

    def _seed_zero_series(self) -> None:
        models = tuple(sorted(MODEL_ROUTES)) + ("unknown",)
        for model in models:
            for outcome, stage in (
                ("allowed", "none"), ("refused", "injection"),
                ("refused", "retrieval"), ("refused", "known_answer"),
                ("refused", "grounding"), ("refused", "pii_egress"),
                ("error", "request"), ("error", "upstream"),
                ("error", "internal"),
            ):
                self.inc("guardrail_requests_total", {
                    "outcome": outcome, "stage": stage, "model": model,
                }, 0)
            for outcome in ("success", "error"):
                self.inc("guardrail_upstream_requests_total", {
                    "model": model, "outcome": outcome,
                }, 0)
            for outcome in ("allowed", "refused", "error"):
                self.observe("guardrail_request_duration_seconds", {
                    "outcome": outcome, "model": model,
                }, 0, count=False)
                self.observe("guardrail_processing_duration_seconds", {
                    "outcome": outcome, "model": model,
                }, 0, count=False)
            self.observe("guardrail_upstream_duration_seconds", {"model": model}, 0, count=False)
            self.observe("guardrail_documents_retrieved", {"model": model}, 0, count=False)
            self.inc("guardrail_answers_declined_total", {"model": model}, 0)
        for stage in (
            "injection_user", "pii_ingress", "canonicalise", "retrieval", "policy",
            "injection_document", "known_answer", "prompt", "grounding", "pii_egress",
            "restore",
        ):
            self.observe("guardrail_stage_duration_seconds", {"stage": stage}, 0, count=False)
        for verdict in ("ok", "flag", "block"):
            self.inc("guardrail_grounding_verdicts_total", {"verdict": verdict}, 0)
            self.observe("guardrail_grounding_overlap_ratio", {"verdict": verdict}, 0, count=False)
        for kind in ("valid", "fabricated", "missing"):
            self.inc("guardrail_citations_total", {"kind": kind}, 0)
        for result in (
            "exact", "semantic", "miss", "skipped_pii", "bypass", "error", "disabled"
        ):
            self.inc("guardrail_semantic_cache_requests_total", {"result": result}, 0)
        for source, action in (("user", "block"), ("document", "drop")):
            self.inc("guardrail_injection_detections_total", {
                "source": source, "action": action,
            }, 0)
        for kind in ("email", "cccd", "phone", "plate", "cmnd", "tax_id", "passport"):
            for direction, action in (("ingress", "redact"), ("egress", "block")):
                self.inc("guardrail_pii_findings_total", {
                    "direction": direction, "kind": kind, "action": action,
                }, 0)
        for reason in ("access", "stale", "injection"):
            self.inc("guardrail_documents_dropped_total", {"reason": reason}, 0)

    def inc(self, name: str, labels: dict[str, str], value: float = 1) -> None:
        values = self._label_values(self.COUNTERS[name], labels)
        with self.lock:
            self.counters[(name, values)] += value

    def add_gauge(self, name: str, labels: dict[str, str], value: float) -> None:
        values = self._label_values(self.gauge_meta[name], labels)
        with self.lock:
            key = (name, values)
            self.gauges[key] = self.gauges.get(key, 0) + value

    def set_gauge(self, name: str, labels: dict[str, str], value: float) -> None:
        values = self._label_values(self.gauge_meta[name], labels)
        with self.lock:
            self.gauges[(name, values)] = value

    def observe(
        self, name: str, labels: dict[str, str], value: float, *, count: bool = True
    ) -> None:
        meta = self.HISTOGRAMS[name]
        values = self._label_values((meta[0], meta[1]), labels)
        with self.lock:
            key = (name, values)
            buckets, total, observations = self.histograms.get(
                key, ([0] * len(meta[2]), 0.0, 0)
            )
            if count:
                for index, boundary in enumerate(meta[2]):
                    if value <= boundary:
                        buckets[index] += 1
                total += value
                observations += 1
            self.histograms[key] = (buckets, total, observations)

    @staticmethod
    def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
        pairs = [f'{name}="{_escape_label(value)}"' for name, value in zip(names, values)]
        if extra:
            pairs.append(extra)
        return "{" + ",".join(pairs) + "}" if pairs else ""

    def render(self) -> bytes:
        lines: list[str] = []
        with self.lock:
            for name, (help_text, label_names) in self.COUNTERS.items():
                lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter"))
                for (metric, values), value in sorted(self.counters.items()):
                    if metric == name:
                        lines.append(f"{name}{self._labels(label_names, values)} {value:g}")
            for name, (help_text, label_names) in self.gauge_meta.items():
                lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
                for (metric, values), value in sorted(self.gauges.items()):
                    if metric == name:
                        lines.append(f"{name}{self._labels(label_names, values)} {value:g}")
            for name, (help_text, label_names, boundaries) in self.HISTOGRAMS.items():
                lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} histogram"))
                for (metric, values), (buckets, total, count) in sorted(self.histograms.items()):
                    if metric != name:
                        continue
                    for boundary, bucket_count in zip(boundaries, buckets):
                        le = f'le="{boundary:g}"'
                        lines.append(
                            f"{name}_bucket{self._labels(label_names, values, le)} "
                            f"{bucket_count}"
                        )
                    lines.append(
                        f'{name}_bucket{self._labels(label_names, values, "le=\"+Inf\"")} {count}'
                    )
                    labels = self._labels(label_names, values)
                    lines.append(f"{name}_sum{labels} {total:g}")
                    lines.append(f"{name}_count{labels} {count}")
        return ("\n".join(lines) + "\n").encode()


_telemetry = Metrics()


def _session(agent: str, access_level: str) -> Session:
    """Keep the spotlight nonce stable so requests in one tenant can share KV prefix."""
    key = (agent, access_level)
    with _sessions_lock:
        if key not in _sessions:
            _sessions[key] = Session(agent=agent, access_level=access_level)
        return _sessions[key]


def _undatamark(answer: str, session: Session) -> str:
    """Take the spotlight marker back out of a model answer.

    Retrieved chunks reach the model DATAMARKED -- every run of whitespace replaced by the
    session marker, so text from a document is visibly not an instruction. Models copy
    phrases from their context, and on 2026-10-07 a live answer came back as
    "Xe^không^được^tính^ AVAILABLE^vì^pin^dưới^ngưỡng..." in front of the analyst pilot.
    spotlight.undatamark existed and nothing called it on the way out.

    Applied BEFORE finalise rather than after, so grounding's lexical overlap is measured
    on the clean text it was calibrated on -- "Xe^không^được" tokenises as one word.

    NOT a PII fix, and worth saying so because it looks like one. The egress scan misses
    a spaced phone number whether or not markers are present -- pii_vi.scan finds nothing
    in "0912 345 678" and nothing in "0912^345^678", only in "0912345678" (checked
    2026-10-07). That is a recall gap in the detector itself, recorded separately.
    """
    if session.mode is spotlight.Mode.DATAMARK and session.marker:
        return spotlight.undatamark(answer, session.marker)
    return answer


def _caller_system(messages: Any) -> str:
    """The calling application's own system prompt, for the plain profile only.

    Under the grounded profile it is discarded on purpose: a caller-supplied system
    prompt could countermand the citation rules, and the MOC copilot's prompt is owned by
    the guardrail. Under the plain profile it is the TASK -- "label this as one of four
    categories", "extract these three fields" -- and dropping it made the profile serve a
    paraphrase instead of the requested output, measured live on 2026-10-07.

    Trust boundary: this text comes from the integrating project, authenticated by its
    virtual key. The USER content it operates on is what stays screened for injection.
    """
    if not isinstance(messages, list):
        return ""
    parts = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            content = message.get("content", "")
            if isinstance(content, str):
                parts.append(content.strip())
            elif isinstance(content, list):
                parts.extend(c.get("text", "").strip() for c in content
                             if isinstance(c, dict) and c.get("type") == "text")
    return "\n\n".join(p for p in parts if p)


def _question(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
    return ""


def _upstream_payload(
    payload: dict[str, Any], prepared: pipeline.PreparedRequest,
    engine_model: str | None = None,
) -> dict[str, Any]:
    assert prepared.prompt is not None
    forwarded = dict(payload)
    # The engine knows base names only. A request for `qwen2.5-7b-plain` has to reach it
    # as `qwen2.5-7b`, or vLLM rejects it as an unknown model -- and the error would
    # surface as a 502 from the guardrail, pointing diagnosis at the engine.
    if engine_model:
        forwarded["model"] = engine_model
    # The response has to be complete before the output guardrails can approve it.
    forwarded["stream"] = False
    forwarded["messages"] = [
        {"role": "system", "content": prepared.prompt.system},
        {"role": "user", "content": prepared.prompt.user},
    ]
    forwarded["cache_salt"] = prepared.prompt.cache_salt
    # setdefault, not assignment: a caller that asked for a specific budget keeps it,
    # including the bench runner, whose whole purpose is to hold output length fixed.
    if DEFAULT_MAX_TOKENS > 0 and not forwarded.get("max_tokens"):
        forwarded["max_tokens"] = DEFAULT_MAX_TOKENS
    # LiteLLM-only metadata is not part of vLLM's OpenAI request schema.
    forwarded.pop("metadata", None)
    _force_citation(forwarded, prepared)
    return forwarded


AUDIT_LOG = os.getenv("AUDIT_LOG", "").lower() in ("1", "true", "yes")


def _audit(trace_id: str, outcome: str, stage: str, model: str,
           elapsed: float, fields: dict[str, Any]) -> None:
    """One JSON line per request, carrying the question and the answer verbatim.

    THIS DELIBERATELY UNDOES THE RULE THE REST OF THE SERVICE KEEPS.

    services/llm_pipeline/tracing.py states it plainly: a span may carry decisions,
    identifiers and counts, and may never carry prompt text, retrieved passage text or
    answer text -- because pipeline stage 2 exists to redact exactly that, and a
    telemetry backend is one more place for it to escape to. LiteLLM is configured the
    same way with turn_off_message_logging. Those are the right defaults and they stay
    the defaults.

    They also leave the operator unable to answer "what did the platform actually send,
    to which model, and what came back", which is not a debugging nicety: without it
    nobody can audit the system they are accountable for, and every claim about its
    behaviour rests on trusting whoever ran the test.

    So this is opt-in, off unless AUDIT_LOG is set, and it writes to stdout rather than
    to the trace backend -- one stream, one retention policy, one thing to turn off.

    WHEN IT IS SAFE TO ENABLE
        The lab corpus is synthetic (data/xanhsm_retrieval_mock), the questions come
        from a versioned eval set, and the cluster is destroyed nightly. Content logging
        here exposes generated text about invented trips.

    WHEN IT IS NOT
        The moment a real MOC question reaches this service. A real question can carry a
        customer id, a phone number, a driver name -- the things stage 2 removes before
        the model sees them, which would then be written here in the clear and shipped
        wherever the cluster's logs go. Turn it off before real traffic, and say so in
        the runbook rather than relying on someone remembering.
    """
    if not AUDIT_LOG:
        return
    try:
        record = {
            "kind": "audit",
            "ts": time.time(),
            "trace_id": trace_id,
            "outcome": outcome,
            "stage": stage,
            "model": model,
            "latency_seconds": round(elapsed, 4),
            "agent": fields.get("agent"),
            "cache_hit": fields.get("cache_hit", False),
            "question": fields.get("question"),
            "model_answer": fields.get("model_answer"),
            "refusal_detail": fields.get("refusal_detail"),
            "pii_inbound": fields.get("pii_inbound"),
            "pii_outbound": fields.get("pii_outbound"),
            "retried": fields.get("retried", False),
            "documents": fields.get("docs"),
            "cited": fields.get("cited"),
            "answer": fields.get("answer"),
            "usage": fields.get("usage"),
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)
    except Exception:  # noqa: BLE001 -- an audit line must never fail a served request
        pass


def _force_citation(
    forwarded: dict[str, Any], prepared: pipeline.PreparedRequest
) -> None:
    """Constrain a weak model's output so it must cite a document that is in context.

    WHY THIS EXISTS, MEASURED RATHER THAN ASSUMED

    The platform's cost story is that the small model handles simple work. On this
    corpus it could not: every benign question routed to qwen2.5-1.5b was refused at the
    grounding stage, so the three agents pinned to it in bench/agents.json were serving
    nobody.

    The cause was not reasoning and not retrieval. Asked "when does a trip count as
    completed", the 1.5B returned all four conditions correctly -- the same answer the 7B
    gave -- and simply omitted `[METRIC-TRIP-001]`. It is an instruction-following
    failure about output FORMAT, and it is uniform: it happens on the easiest
    single-document lookup exactly as often as on multi-step reasoning.

    Worse than omitting one: when the 1.5B did emit a citation unprompted, the id was
    usually not one of the documents it had been given. Over eight single-fact queries,
    two answers carried a citation and NONE of them carried a valid one. The grounding
    stage was right to refuse them; a fabricated citation is the failure this whole
    pipeline exists to prevent.

    Constraining the decode to a regex over the ids actually in context fixes both, and
    costs nothing in answer quality -- measured on 24 queries, 8 per query type:

        query type   citation      valid id       content overlap
        retrieval    2/8 -> 8/8    0/8 -> 8/8     58% -> 66%
        hybrid       6/8 -> 8/8    2/8 -> 8/8     14% -> 20%
        reasoning    4/8 -> 8/8    2/8 -> 8/8      7% -> 15%

    Overlap goes UP under the constraint in every category, so this is not the usual
    trade of quality for structure. It also leaves the complexity ladder visible and
    honest: 66% on single-fact lookups is usable, 20% and 15% are not, which is the
    evidence for routing by question type rather than by agent.

    Deliberately NOT applied to the 7B, which already cites correctly without help.
    Constraining a model that does not need it only adds a way to fail.
    """
    if not FORCE_CITATION_MODELS:
        return
    model = str(forwarded.get("model", ""))
    if model not in FORCE_CITATION_MODELS:
        return
    ids = [doc_id for doc_id in dict.fromkeys(prepared.prompt.cited_ids) if doc_id]
    if not ids:
        # No retrieved context means there is nothing legitimate to cite, and forcing a
        # citation here would be forcing a fabrication.
        return
    alternatives = "|".join(re.escape(doc_id) for doc_id in ids)
    # Prose with no brackets, then one citation drawn from the ids in context. The upper
    # bound is in CHARACTERS and is kept well inside the token budget: the engine must
    # satisfy the whole pattern, so a max_tokens cut before the citation would produce
    # output that matches nothing. 320 characters is roughly 120 Vietnamese tokens
    # against a 192-token default.
    forwarded["structured_outputs"] = {
        "regex": rf"[^\[\]]{{15,320}}\[({alternatives})\]"
    }


def _call_vllm(
    model: str, payload: dict[str, Any], traceparent: str | None = None
) -> dict[str, Any]:
    base = MODEL_ROUTES.get(model)
    if not base:
        raise ValueError(f"model is not routed by guardrail: {model}")
    headers = {"Authorization": "Bearer none", "Content-Type": "application/json"}
    # Hand vLLM the current span as its parent. It only acts on this when started with
    # --otlp-traces-endpoint; otherwise the header is ignored and the engine's time still
    # shows in the waterfall, measured from this side as the upstream span.
    if traceparent:
        headers["traceparent"] = traceparent
    request = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
        return json.loads(response.read())


def _answer(response: dict[str, Any]) -> str:
    try:
        message = response["choices"][0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("vLLM response has no assistant content") from exc
    # A tool call is a well-formed response with content of null, and reporting that as
    # "empty content" sends whoever passed `tools=[...]` looking for a bug in the engine.
    # The grounding and PII checks operate on prose; there is nothing for them to read in
    # a function call, so this platform does not serve one. Say which it is.
    if message.get("tool_calls"):
        raise ValueError(
            "function calling is not supported: the output guardrails check prose for "
            "citations and PII, and a tool call carries neither"
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("vLLM returned empty assistant content")
    return content


def _served(text: str) -> str:
    """The answer as a caller sees it: approved text, plus the demo-data notice.

    Called at every point that EMITS an answer and at no point that stores one. The
    semantic cache and the audit log both keep bare text on purpose: a cache that stored
    the served form would hand back an answer carrying two notices, and an audit log that
    stored it would put the banner into every answer-analysis script downstream.

    Refusals never reach here -- do_POST answers those through _error() and returns -- so
    the notice cannot end up attached to a message that makes no claim about the world.
    """
    if not ANSWER_NOTICE or not text:
        return text
    # Idempotent, so a serve path that is added later and double-wraps is harmless.
    if text.startswith(ANSWER_NOTICE):
        return text
    return f"{ANSWER_NOTICE}\n\n{text}"


def _cached_completion(hit: CacheHit, model: str, started: float) -> dict[str, Any]:
    """An OpenAI-compatible response for a request that never reached the engine."""
    return {
        "id": f"chatcmpl-cache-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": _served(hit.text)},
            "finish_reason": "stop",
        }],
        # No inference happened.  Keeping explicit zeroes prevents a cache hit from being
        # mistaken for missing usage telemetry by clients that aggregate this field.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "guardrail": {
            "verdict": hit.verdict,
            "cited": list(hit.cited),
            "dropped_documents": [],
            "latency_seconds": round(time.monotonic() - started, 6),
            "cache": {"hit": True, "kind": hit.kind,
                      "similarity": round(hit.similarity, 6)},
        },
    }


def _cache_variant(payload: dict[str, Any], query_scope: str | None) -> str:
    """Partition answers whose generation contract differs."""
    # vLLM accepts sampling extensions beyond the OpenAI fields (top_k, min_p,
    # repetition_penalty, guided decoding, and more). An allow-list here would quietly
    # serve an answer generated under a different contract whenever a new option appears.
    # Hash every forwarded option instead, excluding only fields this adapter replaces or
    # that cannot affect generated content.
    ignored = {"messages", "metadata", "model", "stream", "user"}
    values = {name: value for name, value in payload.items() if name not in ignored}
    values["max_tokens"] = values.get("max_tokens") or DEFAULT_MAX_TOKENS
    values["query_scope"] = query_scope or "current"
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid.uuid5(uuid.NAMESPACE_OID, encoded).hex


def _model_label(model: str) -> str:
    # Never allow an arbitrary request field to create unbounded Prometheus series. The
    # profile variants are included because grounded and plain traffic have different
    # refusal profiles and belong in different series -- and the set stays bounded at
    # twice the number of routed models, not at whatever a caller sends.
    if model in MODEL_ROUTES:
        return model
    engine, grounded = _resolve_profile(model)
    return model if (not grounded and engine in MODEL_ROUTES) else "unknown"


def _count(outcome: str, stage: str, model: str) -> None:
    _telemetry.inc("guardrail_requests_total", {
        "outcome": outcome, "stage": stage, "model": model,
    })


def _observe_stage(stage: str, seconds: float) -> None:
    _telemetry.observe("guardrail_stage_duration_seconds", {"stage": stage}, seconds)


def _track_prepared(prepared: pipeline.PreparedRequest, model: str) -> None:
    _telemetry.observe(
        "guardrail_documents_retrieved", {"model": model}, len(prepared.context)
    )
    if prepared.denied_documents:
        _telemetry.inc(
            "guardrail_documents_dropped_total", {"reason": "access"},
            prepared.denied_documents,
        )
    if prepared.stale_documents:
        _telemetry.inc(
            "guardrail_documents_dropped_total", {"reason": "stale"},
            prepared.stale_documents,
        )
    if prepared.dropped_documents:
        _telemetry.inc(
            "guardrail_documents_dropped_total", {"reason": "injection"},
            len(prepared.dropped_documents),
        )
        _telemetry.inc(
            "guardrail_injection_detections_total",
            {"source": "document", "action": "drop"},
            len(prepared.dropped_documents),
        )
    if prepared.inbound_pii:
        for finding in prepared.inbound_pii.findings:
            _telemetry.inc("guardrail_pii_findings_total", {
                "direction": "ingress", "kind": finding.kind, "action": "redact",
            })


def _track_final(final: pipeline.FinalAnswer, model_label: str) -> None:
    report = final.report
    _telemetry.inc("guardrail_grounding_verdicts_total", {"verdict": report.verdict})
    # Only when the answer was actually served. A declined answer that then fails the
    # egress PII scan is a refusal, and counting it here as well would make the two
    # numbers overlap and stop summing to the request total.
    if report.declined and final.ok:
        _telemetry.inc("guardrail_answers_declined_total", {"model": model_label})
    _telemetry.observe(
        "guardrail_grounding_overlap_ratio", {"verdict": report.verdict}, report.overlap
    )
    fabricated = set(report.fabricated)
    valid = sum(citation not in fabricated for citation in report.cited)
    if valid:
        _telemetry.inc("guardrail_citations_total", {"kind": "valid"}, valid)
    if report.fabricated:
        _telemetry.inc(
            "guardrail_citations_total", {"kind": "fabricated"}, len(report.fabricated)
        )
    missing = len(report.uncited_sentences)
    if report.blocked and not report.cited:
        missing += 1
    if missing:
        _telemetry.inc("guardrail_citations_total", {"kind": "missing"}, missing)
    for finding in final.outbound_pii:
        _telemetry.inc("guardrail_pii_findings_total", {
            "direction": "egress", "kind": finding.kind, "action": "block",
        })


def _trace_metrics() -> bytes:
    """Whether the exporter itself is working.

    A trace backend fails quietly by design -- the tracer swallows connection errors so a
    collector outage cannot slow serving -- and quiet failure means nobody notices until
    they go looking for a trace that was never stored. These three counters are how the
    absence of traces becomes visible on the dashboard that is already being watched.
    """
    if not _tracer.enabled:
        return b""
    return (
        "# HELP guardrail_trace_spans_total Spans by what became of them.\n"
        "# TYPE guardrail_trace_spans_total counter\n"
        f'guardrail_trace_spans_total{{result="exported"}} {_tracer.exported}\n'
        f'guardrail_trace_spans_total{{result="dropped"}} {_tracer.dropped}\n'
        f'guardrail_trace_spans_total{{result="failed"}} {_tracer.failures}\n'
    ).encode()


# A refusal names the decision, the observer names the stage that timed it, and for the
# input injection check those two strings differ. Without this the blocking span stays
# green and the waterfall shows a refused request with nothing marked as the cause.
_REFUSAL_TO_SPAN = {"injection": "injection_user"}


def _trace_refusal(
    stage_spans: dict[str, tracing.Span], refusal: pipeline.Refusal
) -> None:
    """Mark the stage that stopped the request, so it is the red bar in the waterfall."""
    span = stage_spans.get(_REFUSAL_TO_SPAN.get(refusal.stage, refusal.stage))
    if span is not None:
        span.fail(refusal.reason)


def _trace_prepared(
    span: tracing.Span, prepared: pipeline.PreparedRequest, agent: str, model: str
) -> None:
    """Attach what the input half decided. Counts and verdicts only -- never content."""
    span.update({
        "guardrail.agent": agent,
        "guardrail.model": model,
        # Which retrieval produced this context. Lexical and hybrid answer the same
        # question differently often enough that a trace without this is ambiguous, and
        # dense can switch itself off at startup without anyone noticing.
        "rag.retrieval_mode": "hybrid" if _dense is not None else "lexical",
        "rag.documents.retrieved": len(prepared.context),
        "rag.documents.denied": prepared.denied_documents,
        "rag.documents.stale": prepared.stale_documents,
        "rag.documents.dropped_injection": len(prepared.dropped_documents),
    })
    if prepared.inbound_pii:
        findings = Counter(finding.kind for finding in prepared.inbound_pii.findings)
        span.set("guardrail.pii.ingress.count", sum(findings.values()))
        # The kinds found, not the values found. Knowing a CCCD was redacted is the whole
        # diagnostic value; knowing which CCCD would undo the redaction.
        span.set("guardrail.pii.ingress.kinds", ",".join(sorted(findings)))
    if prepared.prompt is not None:
        span.set("prompt.cache_salt", prepared.prompt.cache_salt)


def _trace_final(span: tracing.Span, final: pipeline.FinalAnswer) -> None:
    report = final.report
    span.update({
        "guardrail.grounding.verdict": report.verdict,
        "guardrail.grounding.overlap": round(report.overlap, 4),
        "guardrail.citations.cited": len(report.cited),
        "guardrail.citations.fabricated": len(report.fabricated),
        "guardrail.citations.uncited_sentences": len(report.uncited_sentences),
    })
    if final.outbound_pii:
        kinds = sorted({finding.kind for finding in final.outbound_pii})
        span.set("guardrail.pii.egress.count", len(final.outbound_pii))
        span.set("guardrail.pii.egress.kinds", ",".join(kinds))


def _trace_usage(span: tracing.Span, upstream: dict[str, Any]) -> None:
    """Token counts from vLLM's response, which is the only place they are reported."""
    usage = upstream.get("usage")
    if not isinstance(usage, dict):
        return
    for attribute, key in (
        ("llm.usage.prompt_tokens", "prompt_tokens"),
        ("llm.usage.completion_tokens", "completion_tokens"),
        ("llm.usage.total_tokens", "total_tokens"),
    ):
        value = usage.get(key)
        if isinstance(value, int):
            span.set(attribute, value)


class Handler(BaseHTTPRequestHandler):
    server_version = "vllm-bench-guardrail/1"

    # Defensive, and deliberately so. BaseHTTPRequestHandler.handle() loops over
    # handle_one_request, so one instance can serve several requests on one connection --
    # and anything stored on self then outlives the request that set it. Today it cannot
    # happen here, because the default protocol_version is HTTP/1.0 and keep-alive is
    # refused, so every request gets a fresh instance. It is one `protocol_version =
    # "HTTP/1.1"` away from happening, and the symptom would be quiet and confusing:
    # /health answers and the 404 branch below carrying the X-Trace-Id of whatever
    # completion preceded them on the same socket, pointing at a trace that is not
    # theirs. Resetting costs one assignment.
    _trace_id = ""

    def handle_one_request(self) -> None:
        self._trace_id = ""
        super().handle_one_request()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/health/readiness", "/health/liveliness"):
            self._json(HTTPStatus.OK, {"status": "ok", "chunks": len(_chunks)})
            return
        if self.path == "/v1/models":
            self._json(HTTPStatus.OK, {
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "owned_by": "vllm-bench"}
                    for model in sorted(MODEL_ROUTES)
                ],
            })
            return
        if self.path == "/metrics":
            self._metrics()
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        started = time.monotonic()
        model_label = "unknown"
        outcome = "error"
        terminal_stage = "internal"
        upstream_elapsed = 0.0
        # Filled as the request progresses, and read by _audit in the finally block. They
        # stay None on the paths that never reach the corresponding stage.
        audit: dict[str, Any] = {"question": None, "answer": None, "docs": None,
                                 "cited": None, "usage": None, "agent": None,
                                 "cache_hit": False, "pii_inbound": None,
                                 "pii_outbound": None, "retried": False,
                                 "model_answer": None, "refusal_detail": None}

        # Continue the trace LiteLLM started, so one trace covers gateway, guardrail and
        # engine. A caller who sends no traceparent -- curl, the bench runner -- starts a
        # new one here and gets its id back in the X-Trace-Id response header.
        root = _tracer.start(
            "POST /v1/chat/completions",
            self.headers.get("traceparent"),
            tracing.KIND_SERVER,
        )
        self._trace_id = root.trace_id
        spans: list[tracing.Span] = [root]
        stage_spans: dict[str, tracing.Span] = {}

        def observe(stage: str, seconds: float) -> None:
            """Record each pipeline stage as both a histogram sample and a span."""
            _observe_stage(stage, seconds)
            span = _tracer.child_ending_now(root, f"guardrail.{stage}", seconds)
            stage_spans[stage] = span
            spans.append(span)

        _telemetry.add_gauge("guardrail_in_flight_requests", {}, 1)
        try:
            payload = self._read_json()
            model = str(payload.get("model", ""))
            model_label = _model_label(model)
            # Server-side, from the model name the gateway authorised. Never from a
            # request field: see the PLAIN_PROFILE_SUFFIX note.
            engine_model, grounded = _resolve_profile(model)
            question = _question(payload.get("messages"))
            if not model or not question:
                terminal_stage = "request"
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "model and a user message are required",
                )
                return
            if not _routable(model):
                terminal_stage = "request"
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    f"model is not routed by guardrail: {model}",
                )
                return

            # n > 1 IS A GUARDRAIL BYPASS, not merely an unsupported option.
            #
            # pipeline.finalise validates one answer, and the response assembly below
            # rewrites choices[0]. Everything vLLM puts in choices[1:] is forwarded to the
            # caller exactly as the model produced it -- never grounded, never scanned for
            # PII. Measured, not theorised: with n=2 and a planted identity number, the
            # number reached the client through the second choice while the first was
            # checked normally.
            #
            # Refusing is the right fix rather than validating every choice. The pipeline
            # is built around a single answer with a single set of citations, and a
            # request that wants alternatives wants something this platform does not
            # offer. An explicit 400 is a smaller surprise than silently returning one.
            if int(payload.get("n") or 1) > 1:
                terminal_stage = "request"
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "n > 1 is not supported: only the first choice can be guardrailed, "
                    "and returning unchecked alternatives would defeat the output checks",
                )
                return
            # Same reasoning, different field: vLLM's best_of generates n candidates and
            # returns one, which is safe, but any value above 1 combined with n>1 is not.
            if int(payload.get("best_of") or 1) > 1 and payload.get("n") is not None:
                terminal_stage = "request"
                self._error(
                    HTTPStatus.BAD_REQUEST, "invalid_request",
                    "best_of with an explicit n is not supported",
                )
                return

            agent = self.headers.get("X-Agent-Id") or str(payload.get("user") or "default")
            # Access is deployment policy, never a client-controlled request field. A
            # later identity integration may map a verified LiteLLM key to a policy,
            # but accepting metadata/access_level here would be privilege escalation.
            access = DEFAULT_ACCESS_LEVEL
            checked = pipeline.preflight(question, observer=observe)

            # A direct-injection refusal still goes through prepare below so the response,
            # metrics and traces use the same PreparedRequest contract as a cache miss.
            # PII-bearing requests never touch Redis: two callers' different phone numbers
            # both redact to [PHONE_1] and would otherwise collide on the same cache key.
            cache_hit: CacheHit | None = None
            semantic_cache = _get_semantic_cache()
            cache_variant = _cache_variant(
                payload, checked.canonical.scope if checked.canonical is not None else None
            )
            # X-Bypass-Cache: a caller saying "answer this from the engine, not from
            # Redis". It exists because a capacity measurement cannot be made against a
            # cache, and CLEARING the cache first does not achieve that.
            #
            # The eval corpus is 144 questions -- 36 base questions with 4 paraphrases
            # each -- and the semantic threshold is 0.96, close enough that the paraphrases
            # match each other. So a cleared cache refills from the corpus itself within
            # the first couple of hundred requests: at 50 req/s that is about four seconds,
            # after which every request is a hit. Measured on exactly that: a 15,000
            # request run at 50 req/s reached the engine 191 times, and the gateway's
            # blended p95 read 9ms while vLLM's own p95 was 4656ms. Both numbers were
            # correct; only one of them was about the system.
            #
            # A header, not a chart value, because the alternative is `helm upgrade
            # --set semanticCache.enabled=false` from a bench script -- which restarts the
            # guardrail, mutates cluster state for a measurement, and leaves the platform
            # in a different configuration than the one being reported on if the run dies
            # partway.
            #
            # Safe for a client to set: it can only make a request SLOWER and more
            # expensive, never reveal anything. That is the opposite of access_level
            # above, which is refused from the request for exactly that asymmetry.
            bypass_cache = (self.headers.get("X-Bypass-Cache") or "").strip().lower() in (
                "1", "true", "yes",
            )
            cache_eligible = (
                checked.ok
                and not bypass_cache
                and checked.inbound_pii is not None
                and not checked.inbound_pii.findings
                and payload.get("n", 1) == 1
                and not payload.get("tools")
                and not payload.get("logprobs")
            )
            if semantic_cache is None:
                _telemetry.inc("guardrail_semantic_cache_requests_total", {
                    "result": "disabled",
                })
            elif checked.ok and checked.inbound_pii and checked.inbound_pii.findings:
                _telemetry.inc("guardrail_semantic_cache_requests_total", {
                    "result": "skipped_pii",
                })
                root.set("cache.result", "skipped_pii")
            elif checked.ok and not cache_eligible:
                _telemetry.inc("guardrail_semantic_cache_requests_total", {
                    "result": "bypass",
                })
                root.set("cache.result", "bypass")
            elif cache_eligible:
                assert checked.canonical is not None
                try:
                    cache_hit = semantic_cache.lookup(
                        checked.canonical.text, model=model, access_level=access,
                        variant=cache_variant,
                    )
                    cache_result = cache_hit.kind if cache_hit is not None else "miss"
                    _telemetry.inc("guardrail_semantic_cache_requests_total", {
                        "result": cache_result,
                    })
                    root.set("cache.result", cache_result)
                except Exception as exc:  # Redis failure degrades to an ordinary request
                    print(f"semantic cache lookup failed: {type(exc).__name__}", flush=True)
                    _telemetry.inc("guardrail_semantic_cache_requests_total", {
                        "result": "error",
                    })
                    root.set("cache.result", "error")

            if cache_hit is not None:
                audit["cache_hit"] = True
                audit["answer"] = cache_hit.text
                audit["cited"] = list(cache_hit.cited)
                response = _cached_completion(cache_hit, model, started)
                response["guardrail"]["trace_id"] = root.trace_id if root.sampled else ""
                root.update({
                    "cache.hit": True,
                    "cache.kind": cache_hit.kind,
                    "cache.similarity": round(cache_hit.similarity, 6),
                })
                outcome = "allowed"
                terminal_stage = "none"
                if payload.get("stream") is True:
                    self._sse(response, cache_hit.text, model)
                else:
                    self._json(HTTPStatus.OK, response)
                return

            # The REDACTED question, never the raw one.
            #
            # Writing `question` here was a leak I introduced with this log: pipeline
            # stage 2 exists to take identity numbers out of the prompt before the model
            # or any log sees them, and an audit line carrying the original undoes that
            # for every request.
            #
            # Recording the redacted form instead is also the only way the redaction is
            # verifiable from outside. Before this there was a counter saying PII was
            # found and no artefact showing what happened to it -- so "does redaction
            # actually work" could only be answered by trusting the code. Now the log
            # shows `[CCCD_1]` where the number was, and pii_inbound says what kind was
            # replaced, without ever storing the value.
            redacted = checked.inbound_pii
            audit["question"] = redacted.text if redacted else question
            audit["pii_inbound"] = (
                [{"kind": f.kind, "placeholder": f.placeholder} for f in redacted.findings]
                if redacted else []
            )
            audit["agent"] = agent
            prepared = pipeline.prepare(
                question,
                _index,
                _chunks_by_id,
                _session(agent, access),
                policy=policy.Policy(access_level=access),
                dense=_dense,
                observer=observe,
                preflight_result=checked,
                grounded=grounded,
                task=None if grounded else _caller_system(payload.get("messages")),
            )
            _track_prepared(prepared, model_label)
            _trace_prepared(root, prepared, agent, model_label)
            if not prepared.ok:
                assert prepared.refusal is not None
                outcome = "refused"
                terminal_stage = prepared.refusal.stage
                _trace_refusal(stage_spans, prepared.refusal)
                if terminal_stage == "injection":
                    _telemetry.inc(
                        "guardrail_injection_detections_total",
                        {"source": "user", "action": "block"},
                    )
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    prepared.refusal.stage,
                    prepared.refusal.reason,
                    prepared.refusal.detail,
                )
                return

            upstream_started = time.monotonic()
            upstream_outcome = "error"
            upstream_span = _tracer.child(root, f"vllm {model}", tracing.KIND_CLIENT)
            spans.append(upstream_span)
            upstream_span.set("guardrail.model", model_label)
            try:
                upstream = _call_vllm(
                    engine_model,
                    _upstream_payload(payload, prepared, engine_model),
                    traceparent=upstream_span.traceparent(),
                )
                upstream_outcome = "success"
                _trace_usage(upstream_span, upstream)
            except Exception as exc:
                # The class name, never str(exc): an upstream error often quotes the body
                # that caused it, and that body is the user's prompt.
                upstream_span.fail(type(exc).__name__)
                raise
            finally:
                upstream_span.end_ns = time.time_ns()
                upstream_elapsed = time.monotonic() - upstream_started
                _telemetry.observe(
                    "guardrail_upstream_duration_seconds",
                    {"model": model_label}, upstream_elapsed,
                )
                _telemetry.inc("guardrail_upstream_requests_total", {
                    "model": model_label, "outcome": upstream_outcome,
                })
            audit["docs"] = [c.document_id for c in prepared.context]
            audit["usage"] = upstream.get("usage")

            final = pipeline.finalise(_undatamark(_answer(upstream), _session(agent, access)),
                                      prepared, observer=observe)

            # ONE SECOND ATTEMPT WHEN GROUNDING BLOCKS. Measured, after getting this
            # wrong once.
            #
            # 2% of benign questions were refused at every load level, which capped
            # availability at 97.9% against a 99.5% target. The first diagnosis said the
            # model was returning an empty string and a retry-on-empty was written for
            # it. That counter never incremented: `final.text` is "" for EVERY refusal
            # because finalise() sets it, so the log was reporting its own output back.
            #
            # What the model actually produces, once the raw answer is logged separately:
            #
            #     trích dẫn không có trong ngữ cảnh: METRIC-TRIP-001      7 / 16
            #     câu trả lời không trích dẫn tài liệu nào                3 / 16
            #     trích dẫn không có trong ngữ cảnh: FLEET-UTIL-001       2 / 16
            #
            # It cites a document it was not given -- a real corpus id that was not among
            # the retrieved chunks -- or breaks format entirely; one answer emitted a CJK
            # character and then hallucinated a fresh `user` turn. The guardrail is right
            # to block all of it. These are not false refusals in the sense of a wrong
            # verdict; they are correct verdicts on a bad generation.
            #
            # Which is why the fix is a retry and not a looser check. The failure is
            # stochastic, so a second sample usually lands a properly cited answer, and if
            # it does not the request still refuses. Giving up after one attempt charges
            # the caller for the model's variance.
            if (not final.ok and final.refusal is not None
                    and final.refusal.stage == "grounding"):
                _telemetry.inc("guardrail_grounding_retry_total", {"model": model_label})
                retry_span = _tracer.child(root, f"vllm {model} retry", tracing.KIND_CLIENT)
                spans.append(retry_span)
                retry_span.set("guardrail.retry_reason", "grounding")
                try:
                    retried = _call_vllm(
                        engine_model,
                        _upstream_payload(payload, prepared, engine_model),
                        traceparent=retry_span.traceparent(),
                    )
                    second = pipeline.finalise(
                        _undatamark(_answer(retried), _session(agent, access)),
                        prepared, observer=observe)
                    if second.ok:
                        upstream = retried
                        final = second
                        audit["usage"] = upstream.get("usage")
                        audit["retried"] = True
                        _telemetry.inc("guardrail_grounding_retry_rescued_total",
                                       {"model": model_label})
                except Exception:  # noqa: BLE001 -- the first answer already failed
                    retry_span.fail("retry_failed")
                finally:
                    retry_span.end_ns = time.time_ns()
            # The answer BEFORE stage 10 puts the caller's own values back.
            #
            # pipeline.finalise ends by restoring the placeholders this caller supplied,
            # which is correct -- returning someone their own plate number is not a leak,
            # and the egress scan runs before it so nothing can be smuggled past. But
            # `final.text` is therefore the restored text, and writing that here puts the
            # raw value into the log, undoing stage 2 for every request that carried PII.
            #
            # Measured: a question containing 51F-12345 was redacted correctly, reached
            # the model as [PLATE_1], and came back into the audit line in full.
            #
            # Re-applying the mapping in reverse gives the placeholder form, which is what
            # a log should hold: enough to see the shape of the answer, nothing to leak.
            def _placeholders(text: str) -> str:
                if checked.inbound_pii:
                    for ph, original in checked.inbound_pii.mapping.items():
                        text = text.replace(original, ph)
                return text

            audit["answer"] = _placeholders(final.text)
            # WHAT THE MODEL SAID, separately from what was released.
            #
            # finalise() returns FinalAnswer(text="") on every refusal, so `final.text` is
            # empty for a blocked request BY CONSTRUCTION -- not because the model
            # produced nothing. Logging only that field made every refusal look like an
            # empty generation, and I read that back as a diagnosis and built a retry for
            # a failure mode that was not happening. The giveaway was in the same record:
            # `answer` empty while `cited` listed documents, which an empty answer cannot
            # do.
            #
            # A log that cannot distinguish "the model said nothing" from "the guardrail
            # refused what it said" cannot diagnose refusals at all, which is most of what
            # anyone reads this log for.
            audit["model_answer"] = _placeholders(_answer(upstream))
            audit["refusal_detail"] = (
                list(final.refusal.detail) if (not final.ok and final.refusal) else None
            )
            audit["cited"] = list(final.report.cited) if final.report else None
            # Egress findings: identity numbers the model put INTO an answer, which the
            # output stage blocks. Kind only, never the value -- the point is to prove the
            # check fired, not to write the leak into the log that reports it.
            audit["pii_outbound"] = [f.kind for f in final.outbound_pii]
            _track_final(final, model_label)
            _trace_final(root, final)
            if not final.ok:
                assert final.refusal is not None
                outcome = "refused"
                terminal_stage = final.refusal.stage
                _trace_refusal(stage_spans, final.refusal)
                # 400, not 422, and this is a gateway compatibility fix rather than a
                # change of opinion about HTTP. 422 is the better description -- the
                # request was well formed and failed a semantic check downstream -- but
                # LiteLLM v1.90.2 cannot map it: the refusal body has no `model` field, so
                # its openai-provider adapter fails to post-process the response, ends
                # with None, and serves the caller HTTP 200 with a body of `null`.
                #
                # Measured through the gateway, not inferred:
                #   guardrail 400 (injection)  -> LiteLLM 400 + the reason. Correct.
                #   guardrail 422 (grounding)  -> LiteLLM 200 + `null`.     Silent.
                #
                # That is the worst possible failure mode for a guardrail. A refusal
                # becomes an empty success: the caller sees no error, the reason is
                # destroyed, and litellm_proxy_total_requests_metric counts it 200 -- so
                # every dashboard reports the platform as healthy while it answers
                # nobody. It also breaks the safety measurement, because a refused
                # injection arrives indistinguishable from an empty reply.
                #
                # Every request-stage refusal above already uses 400 and travels
                # correctly. This makes the response stage consistent with them, and it
                # matches what OpenAI itself returns for a content-policy refusal.
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    final.refusal.stage,
                    final.refusal.reason,
                    final.refusal.detail,
                )
                return

            # The cache store below and audit["answer"] above both keep `final.text`
            # bare; only what goes out to the caller is wrapped.
            upstream["choices"][0]["message"]["content"] = _served(final.text)
            upstream.setdefault("guardrail", {})
            upstream["guardrail"].update({
                "verdict": final.report.verdict,
                "cited": list(final.report.cited),
                "dropped_documents": prepared.dropped_documents,
                "latency_seconds": round(time.monotonic() - started, 6),
                # Also returned as X-Trace-Id. Present in the body as well so a caller
                # that only keeps the JSON -- the bench runner, a saved curl output --
                # can still find the request in Tempo afterwards.
                "trace_id": root.trace_id if root.sampled else "",
                "cache": {"hit": False},
            })
            if semantic_cache is not None and cache_eligible:
                assert checked.canonical is not None
                try:
                    semantic_cache.store(
                        checked.canonical.text,
                        model=model,
                        access_level=access,
                        text=final.text,
                        cited=final.report.cited,
                        document_ids=[chunk.document_id for chunk in prepared.context],
                        verdict=final.report.verdict,
                        variant=cache_variant,
                    )
                except Exception as exc:  # a cache write is never part of request success
                    print(f"semantic cache store failed: {type(exc).__name__}", flush=True)
            outcome = "allowed"
            terminal_stage = "none"
            if payload.get("stream") is True:
                self._sse(upstream, final.text, model)
            else:
                self._json(HTTPStatus.OK, upstream)
        except json.JSONDecodeError:
            terminal_stage = "request"
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:2000]
            terminal_stage = "upstream"
            self._error(
                HTTPStatus.BAD_GATEWAY,
                "upstream",
                f"vLLM returned HTTP {exc.code}",
                [body],
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            terminal_stage = "upstream"
            reason = exc.reason if hasattr(exc, "reason") else exc
            self._error(
                HTTPStatus.GATEWAY_TIMEOUT,
                "upstream",
                f"vLLM request failed: {reason}",
            )
        except (TypeError, ValueError) as exc:
            terminal_stage = "request"
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            terminal_stage = "internal"
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "guardrail service failed")
        finally:
            elapsed = time.monotonic() - started
            root.end_ns = time.time_ns()
            root.update({
                "guardrail.outcome": outcome,
                "guardrail.terminal_stage": terminal_stage,
                "guardrail.model": model_label,
                "http.route": "/v1/chat/completions",
            })
            if outcome != "allowed":
                # Both a refusal and a crash are errors for the trace backend, which is
                # what makes "show me today's failed requests" a one-click filter in
                # Tempo. The outcome attribute still separates the two.
                root.fail(terminal_stage)
            _tracer.submit(spans)
            _audit(root.trace_id, outcome, terminal_stage, model_label, elapsed, audit)
            _count(outcome, terminal_stage, model_label)
            _telemetry.observe(
                "guardrail_request_duration_seconds",
                {"outcome": outcome, "model": model_label}, elapsed,
            )
            _telemetry.observe(
                "guardrail_processing_duration_seconds",
                {"outcome": outcome, "model": model_label},
                max(0.0, elapsed - upstream_elapsed),
            )
            _telemetry.add_gauge("guardrail_in_flight_requests", {}, -1)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise TypeError("request body must be a JSON object")
        return body

    def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Returned on refusals and errors too -- those are the responses somebody
        # actually wants to look up afterwards. Absent on /health and /metrics, which
        # have no span.
        trace_id = self._trace_id
        if trace_id:
            self.send_header("X-Trace-Id", trace_id)
        self.end_headers()
        self.wfile.write(encoded)

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        detail: list[str] | None = None,
    ) -> None:
        # The stage goes into the MESSAGE as well as `code`, because `code` does not survive
        # the gateway. Measured on LiteLLM v1.90.2, 2026-10-07: a guardrail 400 with
        # code="injection" reaches the caller as
        #
        #   {"error": {"message": "litellm.BadRequestError: OpenAIException - <our message>.
        #              Received Model Group=qwen2.5-7b ...", "type": null, "code": "400"}}
        #
        # `code` is rewritten to the HTTP status and `type` to null; only our message text is
        # carried through, wrapped. Every caller goes through LiteLLM, so without the tag no
        # caller can tell an injection refusal (do not retry) from a grounding refusal (may
        # retry) -- which is the one distinction clients/python/README.md tells them to make.
        # A direct call still gets the structured `code`; the tag is for everyone else.
        self._json(status, {"error": {
            "message": f"[{code}] {message}",
            "type": "guardrail_error",
            "code": code,
            "detail": detail or [],
        }})

    def _sse(self, upstream: dict[str, Any], text: str, model: str) -> None:
        # Applied here rather than at the two call sites so that every streaming path --
        # including one added later -- carries the notice without anyone remembering to.
        # `upstream` contributes only id and created below, so a body whose content field
        # was already served does not double-wrap.
        text = _served(text)
        completion_id = str(upstream.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
        created = int(upstream.get("created") or time.time())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        trace_id = self._trace_id
        if trace_id:
            self.send_header("X-Trace-Id", trace_id)
        self.end_headers()

        def event(delta: dict[str, Any], finish_reason: str | None = None) -> None:
            row = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            self.wfile.write(b"data: " + json.dumps(row, ensure_ascii=False).encode() + b"\n\n")

        event({"role": "assistant", "content": ""})
        for offset in range(0, len(text), 8):
            event({"content": text[offset:offset + 8]})
        event({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _metrics(self) -> None:
        encoded = _telemetry.render() + _trace_metrics()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log request bodies: they may contain the PII step 2 exists to redact.
        print(f'{self.address_string()} - {fmt % args}', flush=True)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"guardrail listening on {HOST}:{PORT}; {len(_chunks)} chunks; "
        f"models={','.join(sorted(MODEL_ROUTES))}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
