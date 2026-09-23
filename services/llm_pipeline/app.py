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
_metrics: Counter[tuple[str, str]] = Counter()
_metrics_lock = threading.Lock()


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


def _count(outcome: str, stage: str = "none") -> None:
    with _metrics_lock:
        _metrics[(outcome, stage)] += 1


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
        try:
            payload = self._read_json()
            model = str(payload.get("model", ""))
            question = _question(payload.get("messages"))
            if not model or not question:
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "model and a user message are required",
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
            )
            if not prepared.ok:
                assert prepared.refusal is not None
                _count("refused", prepared.refusal.stage)
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    prepared.refusal.stage,
                    prepared.refusal.reason,
                    prepared.refusal.detail,
                )
                return

            upstream = _call_vllm(model, _upstream_payload(payload, prepared))
            final = pipeline.finalise(_answer(upstream), prepared)
            if not final.ok:
                assert final.refusal is not None
                _count("refused", final.refusal.stage)
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
            _count("allowed")
            if payload.get("stream") is True:
                self._sse(upstream, final.text, model)
            else:
                self._json(HTTPStatus.OK, upstream)
        except json.JSONDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:2000]
            _count("error", "upstream")
            self._error(
                HTTPStatus.BAD_GATEWAY,
                "upstream",
                f"vLLM returned HTTP {exc.code}",
                [body],
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            _count("error", "upstream")
            reason = exc.reason if hasattr(exc, "reason") else exc
            self._error(
                HTTPStatus.GATEWAY_TIMEOUT,
                "upstream",
                f"vLLM request failed: {reason}",
            )
        except (TypeError, ValueError) as exc:
            _count("error", "request")
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            _count("error", "internal")
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "guardrail service failed")

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
        lines = [
            "# HELP guardrail_requests_total Guardrail decisions by outcome and stage.",
            "# TYPE guardrail_requests_total counter",
        ]
        with _metrics_lock:
            for (outcome, stage), count in sorted(_metrics.items()):
                lines.append(
                    f'guardrail_requests_total{{outcome="{outcome}",stage="{stage}"}} {count}'
                )
        encoded = ("\n".join(lines) + "\n").encode()
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
