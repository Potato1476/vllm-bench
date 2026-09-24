"""W3C trace context and OTLP export for the guardrail service, in the stdlib only.

The service image deliberately carries no web framework and no client libraries, so the
Prometheus registry above it is hand-written too.  The same reasoning applies here: the
OpenTelemetry SDK plus its OTLP exporter is roughly twenty megabytes of dependency to
emit a few hundred bytes of JSON per request, on a node that has already run out of CPU
three times.  Interoperability with Tempo, LiteLLM and vLLM happens at the wire, not in
the process, so a correct encoder is all that is required.

What is implemented, and nothing more:

  * W3C ``traceparent`` parsing and formatting, so a trace started by LiteLLM continues
    through this service and on into vLLM instead of becoming three unrelated traces.
  * Spans with the handful of fields Tempo indexes.
  * OTLP/HTTP with a JSON body, POSTed to ``$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces``.

Note that OTLP/JSON is *not* plain proto3 JSON: the specification overrides the usual
bytes-to-base64 mapping and requires trace and span ids to be lowercase hex, while 64-bit
integers stay strings.  Getting either wrong produces spans Tempo accepts and then cannot
find, which is worse than an outright rejection.

THE RULE THIS MODULE EXISTS TO KEEP
-----------------------------------
A span may carry decisions, identifiers and counts.  A span may never carry prompt text,
retrieved passage text, or answer text.  The pipeline's second stage exists to redact PII
out of exactly that content, and a trace backend is a third place for it to escape to,
after the request log and the cache key.  ``Span.set`` therefore accepts only primitives,
and every call site passes a count or a verdict.  If a future change needs to attach a
sample of text to a span, it needs to answer why the redaction step should be undone for
the convenience of debugging -- and the answer is that it should not.
"""

from __future__ import annotations

import json
import os
import queue
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any

# Span kinds and status codes, from the OTLP specification. Spelled out rather than
# imported because the whole point of this module is to import nothing.
KIND_INTERNAL = 1
KIND_SERVER = 2
KIND_CLIENT = 3

STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_INVALID_TRACE = "0" * 32
_INVALID_SPAN = "0" * 16


def parse_traceparent(header: str | None) -> tuple[str, str, bool] | None:
    """Return (trace_id, parent_span_id, sampled) for a valid W3C header, else None.

    An all-zero id is well-formed but meaningless, and the specification says to treat it
    as absent -- so a caller that sends one gets a fresh trace rather than a trace nobody
    can look up.
    """
    if not header:
        return None
    match = _TRACEPARENT.match(header.strip().lower())
    if not match:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == _INVALID_TRACE or span_id == _INVALID_SPAN:
        return None
    return trace_id, span_id, bool(int(flags, 16) & 0x01)


def format_traceparent(trace_id: str, span_id: str, sampled: bool) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


class Span:
    """One timed operation. Created through ``Tracer.span``, never directly."""

    __slots__ = (
        "attributes", "end_ns", "kind", "name", "parent_id",
        "sampled", "span_id", "start_ns", "status", "status_message", "trace_id",
    )

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        parent_id: str | None,
        name: str,
        kind: int,
        start_ns: int,
        sampled: bool,
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.start_ns = start_ns
        self.end_ns = start_ns
        self.sampled = sampled
        self.attributes: dict[str, Any] = {}
        self.status = STATUS_UNSET
        self.status_message = ""

    def set(self, key: str, value: Any) -> None:
        """Attach one attribute. Primitives only -- see the module docstring."""
        if value is None:
            return
        if not isinstance(value, (str, bool, int, float)):
            raise TypeError(f"span attribute {key!r} must be a primitive, not {type(value)}")
        self.attributes[key] = value

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def fail(self, message: str) -> None:
        self.status = STATUS_ERROR
        # Exception text can quote the input that caused it, so only the class name and
        # a short caller-chosen string ever reach the backend.
        self.status_message = message[:200]

    def traceparent(self) -> str:
        """The header a downstream call should carry to become a child of this span."""
        return format_traceparent(self.trace_id, self.span_id, self.sampled)


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    encoded = []
    for key, value in values.items():
        # bool first: it is a subclass of int, and checking int first would send `true`
        # as `1` and quietly change the type of every boolean attribute.
        if isinstance(value, bool):
            item: dict[str, Any] = {"boolValue": value}
        elif isinstance(value, int):
            item = {"intValue": str(value)}
        elif isinstance(value, float):
            item = {"doubleValue": value}
        else:
            item = {"stringValue": str(value)}
        encoded.append({"key": key, "value": item})
    return encoded


def _encode(spans: list[Span], resource: dict[str, Any]) -> bytes:
    rows = []
    for span in spans:
        row: dict[str, Any] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.name,
            "kind": span.kind,
            "startTimeUnixNano": str(span.start_ns),
            "endTimeUnixNano": str(span.end_ns),
            "attributes": _attributes(span.attributes),
        }
        if span.parent_id:
            row["parentSpanId"] = span.parent_id
        if span.status != STATUS_UNSET:
            row["status"] = {"code": span.status}
            if span.status_message:
                row["status"]["message"] = span.status_message
        rows.append(row)
    body = {
        "resourceSpans": [{
            "resource": {"attributes": _attributes(resource)},
            "scopeSpans": [{
                "scope": {"name": "vllm-bench.guardrail"},
                "spans": rows,
            }],
        }],
    }
    return json.dumps(body, ensure_ascii=False).encode()


class Tracer:
    """Creates spans and ships them to an OTLP collector on a background thread.

    Export never happens on the serving path.  A collector that is slow, restarting or
    absent must cost a request nothing, so finished spans go onto a bounded queue and are
    dropped if it fills.  Losing traces under load is the correct failure: the traces
    exist to explain the service, not to become another way for it to stall.
    """

    def __init__(
        self,
        endpoint: str = "",
        service_name: str = "guardrail",
        sample_ratio: float = 1.0,
        queue_size: int = 2048,
        timeout: float = 5.0,
        resource: dict[str, Any] | None = None,
    ) -> None:
        self.enabled = bool(endpoint)
        self.endpoint = endpoint.rstrip("/") + "/v1/traces" if endpoint else ""
        self.sample_ratio = min(1.0, max(0.0, sample_ratio))
        self.timeout = timeout
        self.dropped = 0
        self.exported = 0
        self.failures = 0
        self._resource = {"service.name": service_name, **(resource or {})}
        self._queue: queue.Queue[list[Span]] = queue.Queue(maxsize=queue_size)
        self._worker: threading.Thread | None = None
        if self.enabled:
            self._worker = threading.Thread(
                target=self._drain, name="otlp-export", daemon=True
            )
            self._worker.start()

    # -- span creation -------------------------------------------------------------

    def start(
        self, name: str, parent_header: str | None = None, kind: int = KIND_SERVER
    ) -> Span:
        """Begin a root span, continuing an upstream trace when one is offered."""
        parent = parse_traceparent(parent_header)
        if parent is not None:
            trace_id, parent_id, sampled = parent
        else:
            trace_id, parent_id = secrets.token_hex(16), None
            # Sample at the root only. Once a trace is under way the decision has been
            # made for it, and re-rolling per service produces traces missing their
            # middle, which look exactly like a service that failed to respond.
            sampled = self.sample_ratio >= 1.0 or random.random() < self.sample_ratio
        return Span(
            trace_id, secrets.token_hex(8), parent_id, name, kind,
            time.time_ns(), sampled and self.enabled,
        )

    def child(self, parent: Span, name: str, kind: int = KIND_INTERNAL) -> Span:
        return Span(
            parent.trace_id, secrets.token_hex(8), parent.span_id, name, kind,
            time.time_ns(), parent.sampled,
        )

    def child_ending_now(self, parent: Span, name: str, seconds: float) -> Span:
        """A child span for work that has already finished and reported its duration.

        ``guardrails.pipeline`` reports each stage through an observer once the stage is
        over, so the start time is reconstructed backwards from the end.  This keeps the
        pipeline free of tracing imports -- its stages stay callable from the offline
        evaluation harness, which has no collector and wants none.
        """
        end = time.time_ns()
        span = Span(
            parent.trace_id, secrets.token_hex(8), parent.span_id, name,
            KIND_INTERNAL, end - int(seconds * 1e9), parent.sampled,
        )
        span.end_ns = end
        return span

    # -- export --------------------------------------------------------------------

    def submit(self, spans: list[Span]) -> None:
        """Hand a finished request's spans to the exporter. Never blocks, never raises."""
        if not self.enabled:
            return
        sampled = [span for span in spans if span.sampled]
        if not sampled:
            return
        try:
            self._queue.put_nowait(sampled)
        except queue.Full:
            self.dropped += len(sampled)

    def _drain(self) -> None:
        while True:
            batch = self._queue.get()
            # Coalesce whatever else is waiting into the same POST. Under load this turns
            # one request-sized payload per trace into one per batch, which matters
            # because each POST is a TCP connection this service opens synchronously.
            while len(batch) < 512:
                try:
                    batch.extend(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._send(batch)

    def _send(self, spans: list[Span]) -> None:
        request = urllib.request.Request(
            self.endpoint,
            data=_encode(spans, self._resource),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                self.exported += len(spans)
        except (urllib.error.URLError, TimeoutError, OSError):
            # A collector that is down must not print a stack trace per request into the
            # log that operators read to find real failures.
            self.failures += len(spans)


def from_environment() -> Tracer:
    """Build the tracer the deployment asks for. No endpoint configured means no tracing."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    resource: dict[str, Any] = {}
    for key, variable in (
        ("service.namespace", "OTEL_SERVICE_NAMESPACE"),
        ("service.version", "OTEL_SERVICE_VERSION"),
        ("k8s.pod.name", "POD_NAME"),
        ("k8s.node.name", "NODE_NAME"),
    ):
        value = os.getenv(variable, "").strip()
        if value:
            resource[key] = value
    return Tracer(
        endpoint=endpoint,
        service_name=os.getenv("OTEL_SERVICE_NAME", "guardrail"),
        sample_ratio=float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0")),
        resource=resource,
    )
