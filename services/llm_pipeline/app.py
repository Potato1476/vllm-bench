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

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "300"))
DEFAULT_ACCESS_LEVEL = os.getenv("DEFAULT_ACCESS_LEVEL", "internal-demo")
CORPUS_PATH = os.getenv("CORPUS_PATH", str(DEFAULT_CORPUS))
MODEL_ROUTES = json.loads(os.getenv(
    "MODEL_ROUTES_JSON",
    json.dumps({
        "qwen2.5-7b": "http://vllm-a.inference.svc.cluster.local:8000/v1",
        "qwen2.5-1.5b": "http://vllm-b.inference.svc.cluster.local:8000/v1",
    }),
))

_chunks = load_chunks(CORPUS_PATH)
_chunks_by_id = {chunk.chunk_id: chunk for chunk in _chunks}
_index = bm25.build([(chunk.chunk_id, chunk.text) for chunk in _chunks])
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
    # LiteLLM-only metadata is not part of vLLM's OpenAI request schema.
    forwarded.pop("metadata", None)
    return forwarded


def _call_vllm(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = MODEL_ROUTES.get(model)
    if not base:
        raise ValueError(f"model is not routed by guardrail: {model}")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer none", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
        return json.loads(response.read())


def _answer(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("vLLM response has no assistant content") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("vLLM returned empty assistant content")
    return content


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


class Handler(BaseHTTPRequestHandler):
    server_version = "vllm-bench-guardrail/1"

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

            agent = self.headers.get("X-Agent-Id") or str(payload.get("user") or "default")
            # Access is deployment policy, never a client-controlled request field. A
            # later identity integration may map a verified LiteLLM key to a policy,
            # but accepting metadata/access_level here would be privilege escalation.
            access = DEFAULT_ACCESS_LEVEL
            prepared = pipeline.prepare(
                question,
                _index,
                _chunks_by_id,
                _session(agent, access),
                policy=policy.Policy(access_level=access),
                observer=_observe_stage,
            )
            _track_prepared(prepared, model_label)
            if not prepared.ok:
                assert prepared.refusal is not None
                outcome = "refused"
                terminal_stage = prepared.refusal.stage
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
            try:
                upstream = _call_vllm(model, _upstream_payload(payload, prepared))
                upstream_outcome = "success"
            finally:
                upstream_elapsed = time.monotonic() - upstream_started
                _telemetry.observe(
                    "guardrail_upstream_duration_seconds",
                    {"model": model_label}, upstream_elapsed,
                )
                _telemetry.inc("guardrail_upstream_requests_total", {
                    "model": model_label, "outcome": upstream_outcome,
                })
            final = pipeline.finalise(_answer(upstream), prepared, observer=_observe_stage)
            _track_final(final)
            if not final.ok:
                assert final.refusal is not None
                outcome = "refused"
                terminal_stage = final.refusal.stage
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
            })
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
        encoded = _telemetry.render()
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
