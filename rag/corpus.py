"""Loading the XanhSM retrieval corpus, and the metadata contract the rest depends on.

The corpus ships 798 chunks whose metadata is doing real work, not decoration:

  status         active | deprecated | draft
  access_level   internal-demo | public
  category       daily-operations (720), knowledge-base (30), metric-catalog (15), ...
  effective_date when the definition took effect
  document_id    the unit `must_cite` names in the evaluation set

`status` is the important one. Four documents are deliberate traps -- OLD-METRIC-ACTIVE-2025,
OLD-METRIC-CANCEL-2025, OLD-GEO-001, DRAFT-UTIL-002 -- and each one discusses exactly the
topic of a correct answer: the old Active Driver rule, the old Cancellation Rate, the old
city-assignment rule, an unapproved Utilization change. Semantically they are near-identical
to the documents that should win. No embedding model separates them, because there is
nothing in the meaning to separate: the difference is that one is in force and one is not,
and that fact lives only in the metadata.

DRAFT-UTIL-002 states the requirement in its own body text: the retrieval system must
prefer the approved definitions over the draft. So this is the corpus telling us what the
guardrail is for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .text_vi import normalise

DEFAULT_CORPUS = Path("data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl")
DEFAULT_EVAL = Path("data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    text: str
    category: str
    status: str
    access_level: str
    effective_date: str | None
    source_url: str | None
    version: str | None
    tags: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return self.status == "active"


def load_chunks(path: Path | str = DEFAULT_CORPUS) -> list[Chunk]:
    out: list[Chunk] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        m = row.get("metadata") or {}
        tags = m.get("tags") or []
        out.append(Chunk(
            chunk_id=row["id"],
            document_id=m.get("document_id", row["id"]),
            # Normalised at load so the index, the reranker and the prompt all see one
            # form. Doing it later, in only some of those places, is how prefix caching
            # silently stops working.
            text=normalise(row.get("text", "")),
            category=m.get("category", "unknown"),
            status=m.get("status", "active"),
            access_level=m.get("access_level", "internal-demo"),
            effective_date=m.get("effective_date"),
            source_url=m.get("source_url"),
            version=m.get("version"),
            tags=tuple(tags if isinstance(tags, list) else [tags]),
        ))
    return out


@dataclass(frozen=True)
class EvalQuery:
    query_id: str
    base_question_id: str
    query: str
    query_type: str
    relevant_chunk_ids: tuple[str, ...]
    relevant_document_ids: tuple[str, ...]
    must_cite: tuple[str, ...]
    expected_answer: str


def load_eval(path: Path | str = DEFAULT_EVAL) -> list[EvalQuery]:
    out: list[EvalQuery] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append(EvalQuery(
            query_id=r["query_id"],
            base_question_id=r.get("base_question_id", r["query_id"]),
            query=normalise(r["query"]),
            query_type=r.get("query_type", "retrieval"),
            relevant_chunk_ids=tuple(r.get("relevant_chunk_ids") or []),
            relevant_document_ids=tuple(r.get("relevant_document_ids") or []),
            must_cite=tuple(r.get("must_cite") or []),
            expected_answer=r.get("expected_answer", ""),
        ))
    return out
