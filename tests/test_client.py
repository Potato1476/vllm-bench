"""Tests for the client the other DA teams will copy.

Run without the `openai` package installed, by stubbing it the same way
test_llm_pipeline.py stubs redis. The parsing logic under test is pure; only the import
needs openai to exist.

Worth testing at all because the notice-splitting is a heuristic. If it over-matches it
eats the first paragraph of a real answer, and the team reading `.answer` loses content
with nothing to indicate it happened.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

CLIENT_DIR = Path(__file__).resolve().parents[1] / "clients" / "python"


class _FakeBadRequest(Exception):
    def __init__(self, body: dict) -> None:
        super().__init__("400")
        self.body = body


def _load():
    """Import moc_copilot with a stubbed openai, fresh each time."""
    stub = types.SimpleNamespace(OpenAI=object, BadRequestError=_FakeBadRequest)
    with mock.patch.dict(sys.modules, {"openai": stub}):
        sys.path.insert(0, str(CLIENT_DIR))
        try:
            sys.modules.pop("moc_copilot", None)
            import moc_copilot
            return moc_copilot
        finally:
            sys.path.remove(str(CLIENT_DIR))


class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Resp:
    def __init__(self, content, extra=None):
        self.choices = [_Choice(content)]
        self.model_extra = extra or {}
        self.usage = None


NOTICE = ("⚠️ Dữ liệu demo: corpus đang dùng là dữ liệu giả lập, không phải dữ liệu "
          "nội bộ Xanh SM.")
ANSWER = "Chuyến hoàn thành khi trip_status = COMPLETED [METRIC-TRIP-001]."


class NoticeSplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = _load()

    def test_notice_is_split_off_and_body_keeps_it(self) -> None:
        r = self.m._answer(_Resp(f"{NOTICE}\n\n{ANSWER}"))
        self.assertEqual(r.notice, NOTICE)
        self.assertEqual(r.answer, ANSWER)
        # .body is what a UI shows, so the warning must still be in it.
        self.assertIn(NOTICE, r.body)

    def test_an_answer_without_a_notice_is_left_whole(self) -> None:
        """The state after the corpus becomes real and ANSWER_NOTICE is cleared."""
        r = self.m._answer(_Resp(ANSWER))
        self.assertEqual(r.notice, "")
        self.assertEqual(r.answer, ANSWER)

    def test_a_multi_paragraph_answer_does_not_lose_its_first_paragraph(self) -> None:
        """The failure this heuristic could cause, pinned.

        A normal answer whose first paragraph happens to be followed by a blank line must
        not have that paragraph mistaken for the notice and stripped from .answer.
        """
        body = "Chuyến hoàn thành gồm hai điều kiện [METRIC-TRIP-001].\n\nThứ nhất, ..."
        r = self.m._answer(_Resp(body))
        self.assertEqual(r.notice, "")
        self.assertEqual(r.answer, body)

    def test_citations_and_cache_flag_are_read_when_present(self) -> None:
        extra = {"guardrail": {"cited": ["METRIC-TRIP-001"], "cache": {"hit": True}}}
        r = self.m._answer(_Resp(ANSWER, extra))
        self.assertEqual(r.cited, ("METRIC-TRIP-001",))
        self.assertTrue(r.cached)

    def test_a_stripped_guardrail_block_is_not_an_error(self) -> None:
        """LiteLLM may drop extension fields; an answer is still an answer."""
        r = self.m._answer(_Resp(ANSWER))
        self.assertEqual(r.cited, ())
        self.assertTrue(r.ok)


class RefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = _load()

    def test_stage_and_detail_are_read_from_the_error_body(self) -> None:
        exc = _FakeBadRequest({"error": {
            "code": "injection", "message": "direct injection", "detail": ["rule-3"]}})
        r = self.m._refusal(exc)
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "injection")
        self.assertEqual(r.detail, ("rule-3",))

    def test_input_refusals_are_separated_from_output_refusals(self) -> None:
        """Retrying is pointless for one group and may help for the other."""
        for stage in ("injection", "pii_ingress", "policy"):
            with self.subTest(stage=stage):
                self.assertTrue(self.m._refusal(
                    _FakeBadRequest({"error": {"code": stage}})).input_was_rejected)
        for stage in ("grounding", "pii_egress"):
            with self.subTest(stage=stage):
                self.assertFalse(self.m._refusal(
                    _FakeBadRequest({"error": {"code": stage}})).input_was_rejected)

    def test_the_stage_survives_litellm_rewriting_the_error(self) -> None:
        """The body below is what LiteLLM v1.90.2 actually returned on 2026-10-07 for a
        guardrail injection refusal: code rewritten to "400", type null, our message
        wrapped. Before the [stage] tag, this parsed as stage "unknown", and an injection
        looked retryable."""
        exc = _FakeBadRequest({"error": {
            "message": "litellm.BadRequestError: OpenAIException - [injection] câu hỏi chứa "
                       "chỉ dẫn nhằm ghi đè hệ thống. Received Model Group=qwen2.5-7b\n"
                       "Available Model Group Fallbacks=None",
            "type": None, "param": None, "code": "400"}})
        r = self.m._refusal(exc)
        self.assertEqual(r.stage, "injection")
        self.assertTrue(r.input_was_rejected)

    def test_an_unknown_bracketed_word_is_not_taken_for_a_stage(self) -> None:
        """A bracket in ordinary text -- a document id, say -- must not become a stage."""
        exc = _FakeBadRequest({"error": {"code": "400",
                                         "message": "loi gi do [METRIC-TRIP-001] [foo]"}})
        self.assertEqual(self.m._refusal(exc).stage, "unknown")

    def test_a_malformed_error_body_still_yields_a_refusal(self) -> None:
        r = self.m._refusal(_FakeBadRequest({}))
        self.assertEqual(r.stage, "unknown")
        self.assertFalse(r.ok)


class AgentIdTest(unittest.TestCase):
    def test_an_empty_agent_id_is_refused_at_construction(self) -> None:
        """Without it every per-project dashboard is empty and nothing reports an error,
        so the only place to catch it is before the first request."""
        m = _load()
        with self.assertRaises(ValueError):
            m.MocCopilot(base_url="http://x/v1", api_key="sk-x", agent_id="")


class RosterTest(unittest.TestCase):
    def test_agents_json_matches_the_roster_named_in_the_brief(self) -> None:
        """TC4 counts those projects, so the ids have to be theirs and not ours."""
        import json
        path = Path(__file__).resolve().parents[1] / "bench" / "agents.json"
        ids = {a["id"] for a in json.loads(path.read_text(encoding="utf-8"))["agents"]}
        self.assertTrue({f"da{n}" for n in (19, 20, 32, 39, 41, 44, 45)} <= ids)


if __name__ == "__main__":
    unittest.main()
