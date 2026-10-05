"""A thin client for the MOC serving platform, for the DA teams that consume it.

WHY THIS EXISTS WHEN THE PLATFORM IS ALREADY OPENAI-COMPATIBLE

It is compatible, and a team can ignore this file entirely -- `OpenAI(base_url=...,
api_key=...)` works. This wrapper exists because three differences are silent, and each
one costs a team an afternoon to find:

  1. PER-PROJECT ATTRIBUTION NEEDS A HEADER, NOT THE `user` FIELD. LiteLLM v1.90.2 does
     not populate its end_user dimension from the OpenAI `user` body field -- measured on
     this deployment, every series came back end_user="None". The platform reads
     X-Agent-Id instead. A team that sets `user="da32"` and waits for its dashboard to
     fill gets an empty dashboard and no error.

  2. A GUARDRAIL REFUSAL ARRIVES AS HTTP 400. The OpenAI SDK raises BadRequestError, which
     most code treats as "I built the request wrong" and retries. It is not that. It is a
     final policy decision about the CONTENT, and retrying spends GPU to be refused again.
     Refused(...) below makes the difference checkable instead of guessable.

  3. THE ANSWER CARRIES A DATA-PROVENANCE NOTICE. Every answer is prefixed with a line
     saying the corpus is simulated. Code that parses answers positionally will trip over
     it. `.body` gives the answer with the notice, `.answer` without.

Requires only the `openai` package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

try:
    from openai import BadRequestError, OpenAI
except ImportError as exc:  # pragma: no cover - a clearer message than a traceback
    raise SystemExit("can cai dat: pip install openai") from exc

# Stages the guardrail can refuse at. Reported as `error.code` on a 400, and worth
# distinguishing: an `injection` refusal means the INPUT was rejected and the caller should
# look at what it sent, while `grounding` means the model answered without usable support
# and the same question may well succeed on a retry.
REFUSAL_STAGES = frozenset({
    "injection",
    "pii_ingress",
    "retrieval",
    "policy",
    "known_answer",
    "grounding",
    "pii_egress",
})

# Separates the provenance notice from the answer. Kept in sync with ANSWER_NOTICE in
# services/llm_pipeline/app.py, which prepends it followed by a blank line.
_NOTICE_SEP = "\n\n"


@dataclass
class Answer:
    """A served answer. `ok` is True."""

    body: str
    """Exactly what the platform returned, provenance notice included. Show this to a
    person: the notice is the thing that stops a simulated figure reaching a real report."""

    notice: str = ""
    """The provenance line, split off. Always present on this platform: its corpus is
    simulated permanently, so an empty value here means the warning went missing."""

    cited: tuple[str, ...] = ()
    """Document ids the answer leaned on, when the platform reports them."""

    cached: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    ok: bool = True

    @property
    def answer(self) -> str:
        """The answer without the notice. For parsing, never for display."""
        return self.body[len(self.notice) + len(_NOTICE_SEP):] if self.notice else self.body


@dataclass
class Refused:
    """The platform declined on purpose. `ok` is False. DO NOT RETRY blindly.

    A refusal is a decision about content, not a transport failure. Retrying an
    `injection` refusal sends the same rejected input again; retrying `grounding` may
    help, but the platform already retries that internally once before giving up.
    """

    stage: str
    message: str
    detail: tuple[str, ...] = ()
    ok: bool = False

    @property
    def input_was_rejected(self) -> bool:
        """True when the refusal is about what WE sent, so retrying cannot help."""
        return self.stage in {"injection", "pii_ingress", "policy", "invalid_request"}


class MocCopilot:
    """Point this at the platform and use `.ask()`.

    agent_id must be the project's own id from bench/agents.json (da19, da32, ...).
    It is what every per-project latency, token and cost panel groups by, and the
    platform cannot infer it.
    """

    def __init__(self, base_url: str, api_key: str, agent_id: str,
                 model: str = "qwen2.5-7b", timeout: float = 60.0):
        if not agent_id:
            raise ValueError(
                "agent_id la bat buoc -- khong co no thi moi dashboard theo du an deu rong, "
                "va khong co loi nao bao cho ban biet"
            )
        self.agent_id = agent_id
        self.model = model
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            # Sent on every request rather than per call, because the one that gets
            # forgotten is the one that silently stops being attributed.
            default_headers={"X-Agent-Id": agent_id},
        )

    @property
    def openai(self) -> OpenAI:
        """The underlying SDK client, headers already set.

        Use it directly for anything this wrapper does not cover. Everything the OpenAI
        SDK can do against a chat-completions endpoint works here.
        """
        return self._client

    def ask(self, question: str, *, model: str | None = None,
            system: str | None = None, **kwargs: Any) -> Answer | Refused:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": question})
        try:
            response = self._client.chat.completions.create(
                model=model or self.model, messages=messages, **kwargs,
            )
        except BadRequestError as exc:
            return _refusal(exc)
        return _answer(response)

    def stream(self, question: str, *, model: str | None = None,
               **kwargs: Any) -> Iterator[str]:
        """Yields text chunks.

        It will NOT feel like token-by-token streaming, and that is deliberate upstream:
        the guardrail buffers the whole answer and checks it before releasing anything,
        because a PII leak or a fabricated citation cannot be recalled once sent. Expect
        a pause and then the text. A spinner is a better UI here than a typing cursor.
        """
        stream = self._client.chat.completions.create(
            model=model or self.model,
            messages=[{"role": "user", "content": question}],
            stream=True,
            **kwargs,
        )
        for chunk in stream:
            piece = chunk.choices[0].delta.content if chunk.choices else None
            if piece:
                yield piece

    def models(self) -> list[str]:
        return [m.id for m in self._client.models.list().data]


def _refusal(exc: BadRequestError) -> Refused:
    body = getattr(exc, "body", None) or {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    stage = err.get("code") or "unknown"
    return Refused(
        stage=stage,
        message=err.get("message") or str(exc),
        detail=tuple(err.get("detail") or ()),
    )


def _answer(response: Any) -> Answer:
    body = response.choices[0].message.content or ""

    # The guardrail block is an extension field, so whether it survives the gateway is a
    # property of the deployment rather than something to assume. Read it if it is there
    # and carry on without it if not -- an answer is still an answer.
    extra = getattr(response, "model_extra", None) or {}
    guardrail = extra.get("guardrail") if isinstance(extra, dict) else None
    cited: tuple[str, ...] = ()
    cached = False
    if isinstance(guardrail, dict):
        cited = tuple(guardrail.get("cited") or ())
        cached = bool((guardrail.get("cache") or {}).get("hit"))

    # Split the notice by looking for a first paragraph that is not part of the answer.
    # Done by shape rather than by matching the exact wording, so a reworded notice does
    # not quietly end up inside `.answer`.
    notice = ""
    head, sep, _rest = body.partition(_NOTICE_SEP)
    if sep and ("⚠" in head or head.lower().startswith(("luu y", "lưu ý", "môi trường"))):
        notice = head

    usage = {}
    if getattr(response, "usage", None):
        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else {}

    return Answer(body=body, notice=notice, cited=cited, cached=cached,
                  usage=usage, raw=response)
