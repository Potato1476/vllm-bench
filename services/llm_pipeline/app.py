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

from guardrails import pipeline
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
CORPUS_PATH = os.getenv("CORPUS_PATH", str(DEFAULT_CORPUS))
MODEL_ROUTES = json.loads(os.getenv(
    "MODEL_ROUTES_JSON",
    json.dumps({
        "qwen2.5-7b": "http://vllm-a.inference.svc.cluster.local:8000/v1",
        "qwen2.5-1.5b": "http://vllm-b.inference.svc.cluster.local:8000/v1",
    }),
))

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
        ranker = dense_mod.DenseRankerAdapter(
            index, dense_mod.HttpBackend(DENSE_ENDPOINT, timeout=DENSE_TIMEOUT)
        )
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
    """Connect to the pod-local Redis sidecar, or leave serving uncached."""
    if not _semantic_cache_configured:
        return None
    try:
        import redis

        socket = os.getenv("REDIS_UNIX_SOCKET", "/run/redis/redis.sock")
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
        print(f"semantic cache on: redis unix socket {socket}", flush=True)
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
        "guardrail_documents_dropped_total": (
            "Retrieved documents excluded from the prompt by reason.",
            ("reason",),
        ),
        "guardrail_grounding_verdicts_total": (
            "Grounding checks by verdict.",
            ("verdict",),
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
    payload: dict[str, Any], prepared: pipeline.PreparedRequest
) -> dict[str, Any]:
    assert prepared.prompt is not None
    forwarded = dict(payload)
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
    return forwarded


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


def _cached_completion(hit: CacheHit, model: str, started: float) -> dict[str, Any]:
    """An OpenAI-compatible response for a request that never reached the engine."""
    return {
        "id": f"chatcmpl-cache-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": hit.text},
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
    # Never allow an arbitrary request field to create unbounded Prometheus series.
    return model if model in MODEL_ROUTES else "unknown"


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


def _track_final(final: pipeline.FinalAnswer) -> None:
    report = final.report
    _telemetry.inc("guardrail_grounding_verdicts_total", {"verdict": report.verdict})
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
            question = _question(payload.get("messages"))
            if not model or not question:
                terminal_stage = "request"
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "model and a user message are required",
                )
                return
            if model not in MODEL_ROUTES:
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
            cache_eligible = (
                checked.ok
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

            prepared = pipeline.prepare(
                question,
                _index,
                _chunks_by_id,
                _session(agent, access),
                policy=policy.Policy(access_level=access),
                dense=_dense,
                observer=observe,
                preflight_result=checked,
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
                    model,
                    _upstream_payload(payload, prepared),
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
            final = pipeline.finalise(_answer(upstream), prepared, observer=observe)
            _track_final(final)
            _trace_final(root, final)
            if not final.ok:
                assert final.refusal is not None
                outcome = "refused"
                terminal_stage = final.refusal.stage
                _trace_refusal(stage_spans, final.refusal)
                self._error(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    final.refusal.stage,
                    final.refusal.reason,
                    final.refusal.detail,
                )
                return

            upstream["choices"][0]["message"]["content"] = final.text
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
        self._json(status, {"error": {
            "message": message,
            "type": "guardrail_error",
            "code": code,
            "detail": detail or [],
        }})

    def _sse(self, upstream: dict[str, Any], text: str, model: str) -> None:
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
