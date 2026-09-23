"""The request path, with every guardrail in the order its failure mode requires.

    question
      |
      1  injection.inspect(source="user")     BLOCK  -> refuse, nothing downstream runs
      2  pii_vi.redact(inbound)               redact before retrieval and before logging
      3  canonical.canonicalise               strip lead-ins; extract scope markers
      4  retrieve.hybrid                      lexical (+ dense when available)
      5  policy.apply                         access: drop. currency: withhold + advise
      6  injection.inspect(source="document") BLOCK a poisoned chunk, keep the rest
      6b known_answer.check                   optional, costs one generation; catches what
                                              no pattern can, see the module docstring
      7  prompt.build                         stable prefix, datamarking, cache salt
      |
    generate
      |
      8  grounding.check                      fabricated citation -> block
      9  pii_vi.scan(outbound)                identity numbers in the answer -> block
      10 restore inbound placeholders         only values the caller themselves supplied

ORDER IS THE DESIGN. Two placements are worth stating because getting them wrong is the
usual mistake.

PII redaction sits BEFORE retrieval, not after. Redacting only on the way to the model
leaves the raw value in the retrieval query, the request log and any trace exporter --
three copies outside the model that nobody thinks of as an LLM problem.

Document-level injection screening sits AFTER policy, not before. Screening 50 retrieval
candidates costs 50 scans to protect the 5 that survive policy. Screening after means the
work is proportional to what actually reaches the prompt.

Step 6 drops the offending chunk rather than failing the request. A poisoned document in
the corpus should not be able to deny service to every question that happens to retrieve
it, which is the cheapest attack of all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from prompt.build import BuiltPrompt, Session, build
from prompt.canonical import CanonicalQuery, canonicalise
from rag import bm25, policy as pol, retrieve
from rag.corpus import Chunk

from . import grounding, injection, known_answer, pii_vi

StageObserver = Callable[[str, float], None]


def _observe(observer: StageObserver | None, stage: str, started: float) -> None:
    if observer is not None:
        observer(stage, time.perf_counter() - started)


@dataclass
class Refusal:
    stage: str
    reason: str
    detail: list[str] = field(default_factory=list)


@dataclass
class PreparedRequest:
    prompt: BuiltPrompt | None
    canonical: CanonicalQuery | None
    context: list[Chunk] = field(default_factory=list)
    dropped_documents: list[str] = field(default_factory=list)
    inbound_pii: pii_vi.Redaction | None = None
    advisory: str | None = None
    denied_documents: int = 0
    stale_documents: int = 0
    refusal: Refusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.prompt is not None


def prepare(
    question: str,
    index: bm25.Bm25Index,
    chunks_by_id: dict[str, Chunk],
    session: Session,
    *,
    policy: pol.Policy | None = None,
    dense: retrieve.DenseRanker | None = None,
    classifier: injection.InjectionClassifier | None = None,
    detector: known_answer.Completion | None = None,
    top_k: int = 5,
    observer: StageObserver | None = None,
) -> PreparedRequest:
    # 1 -- direct injection
    started = time.perf_counter()
    verdict = injection.inspect(question, source="user", classifier=classifier)
    _observe(observer, "injection_user", started)
    if verdict.blocked:
        return PreparedRequest(
            prompt=None, canonical=None,
            refusal=Refusal("injection", "câu hỏi chứa chỉ dẫn nhằm ghi đè hệ thống",
                            [s.rule for s in verdict.signals]))

    # 2 -- inbound PII, before anything persists it
    started = time.perf_counter()
    red = pii_vi.redact(question)
    _observe(observer, "pii_ingress", started)

    # 3 -- canonical form drives both retrieval and the cache key
    started = time.perf_counter()
    canon = canonicalise(red.text)
    _observe(observer, "canonicalise", started)

    # A question explicitly about superseded rules needs them unsuppressed, which is why
    # canonicalise extracts the marker instead of deleting it.
    effective = policy or pol.Policy()
    if canon.scope in ("historical", "draft"):
        effective = pol.Policy(access_level=effective.access_level, require_current=False)

    # 4, 5 -- retrieve, then filter by rights and currency
    started = time.perf_counter()
    hits = retrieve.hybrid(canon.text, index, chunks_by_id, dense=dense,
                           candidates=50, top_k=top_k * 3)
    _observe(observer, "retrieval", started)
    started = time.perf_counter()
    result = pol.apply([h.chunk for h in hits], effective)
    _observe(observer, "policy", started)

    # 6 -- indirect injection, on what survived
    started = time.perf_counter()
    context: list[Chunk] = []
    dropped: list[str] = []
    for chunk in result.allowed:
        if injection.inspect(chunk.text, source="document",
                             classifier=classifier).blocked:
            dropped.append(chunk.document_id)
            continue
        context.append(chunk)
        if len(context) >= top_k:
            break
    _observe(observer, "injection_document", started)

    if not context:
        return PreparedRequest(
            prompt=None, canonical=canon, dropped_documents=dropped,
            inbound_pii=red, denied_documents=result.denied_count,
            stale_documents=len(result.suppressed),
            refusal=Refusal("retrieval", "không tìm thấy tài liệu phù hợp còn hiệu lực"))

    # 6b -- known-answer detection, opt-in because it costs a full extra prefill.
    #
    # Refuses the whole request rather than dropping a chunk: this check runs over the
    # concatenated context and reports only that SOMETHING in it diverted the model, so
    # there is no chunk to drop. Localising it costs one generation per chunk, which at
    # five chunks is five extra prefills to arrive at the same refusal.
    if detector is not None:
        started = time.perf_counter()
        for d in known_answer.check_chunks(
                [(c.document_id, c.text) for c in context], detector):
            if d.compromised:
                _observe(observer, "known_answer", started)
                return PreparedRequest(
                    prompt=None, canonical=canon, context=context,
                    dropped_documents=dropped,
                    inbound_pii=red, denied_documents=result.denied_count,
                    stale_documents=len(result.suppressed),
                    refusal=Refusal("known_answer",
                                    "dữ liệu truy hồi làm mô hình đi chệch chỉ dẫn",
                                    [d.scope]))
        _observe(observer, "known_answer", started)

    # 7 -- assemble
    advisory = pol.currency_advisory(result)
    started = time.perf_counter()
    prompt = build(canon.text, context, session, advisory=advisory)
    _observe(observer, "prompt", started)
    return PreparedRequest(prompt=prompt, canonical=canon, context=context,
                           dropped_documents=dropped, inbound_pii=red,
                           advisory=advisory, denied_documents=result.denied_count,
                           stale_documents=len(result.suppressed))


@dataclass
class FinalAnswer:
    text: str
    report: grounding.GroundingReport
    outbound_pii: list[pii_vi.Finding] = field(default_factory=list)
    refusal: Refusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def finalise(
    answer: str,
    prepared: PreparedRequest,
    *,
    observer: StageObserver | None = None,
) -> FinalAnswer:
    # 8 -- citations must exist in the context that was actually sent
    started = time.perf_counter()
    report = grounding.check(answer, prepared.context)
    _observe(observer, "grounding", started)
    if report.blocked:
        return FinalAnswer(text="", report=report,
                           refusal=Refusal("grounding", "câu trả lời không có căn cứ",
                                           report.notes))

    # 9 -- identity numbers must not leave, whatever their origin
    started = time.perf_counter()
    leaked = pii_vi.scan(answer)
    _observe(observer, "pii_egress", started)
    if leaked:
        kinds = sorted({f.kind for f in leaked})
        return FinalAnswer(text="", report=report, outbound_pii=leaked,
                           refusal=Refusal("pii_egress",
                                           "câu trả lời chứa dữ liệu định danh", kinds))

    # 10 -- give the caller back only what the caller supplied
    started = time.perf_counter()
    text = prepared.inbound_pii.restore(answer) if prepared.inbound_pii else answer
    _observe(observer, "restore", started)
    return FinalAnswer(text=text, report=report)
