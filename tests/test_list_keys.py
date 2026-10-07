"""Tests for the key listing, specifically the half that compares two sources.

The listing itself is hard to get wrong. The comparison is the part worth testing, and it
is also the part that only does anything on the day the two sides disagree -- which is any
day Aurora was recreated and `make agent-keys` has not run yet. A silent bug here would
show up as a confident "everything fine" on exactly the morning it is not.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.scripts import list_keys  # noqa: E402


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _run(live_rows, secret_data):
    """Drive main() with both sources stubbed, returning (exit code, printed text)."""
    argv = ["list_keys.py", "--base-url", "http://x", "--master-key", "sk-master"]
    buf = io.StringIO()
    with (mock.patch.object(list_keys, "_call", return_value={"keys": live_rows}),
          mock.patch.object(list_keys, "secret_aliases", return_value=secret_data),
          mock.patch.object(sys, "argv", argv),
          redirect_stdout(buf)):
        code = list_keys.main()
    return code, buf.getvalue()


def _key(alias, spend=0.0):
    return {"key_alias": alias, "models": ["qwen2.5-7b"], "max_budget": 10.0, "spend": spend}


class ComparisonTest(unittest.TestCase):
    def test_both_sides_agree_is_a_clean_exit(self) -> None:
        code, out = _run([_key("da19"), _key("da32")], {"da19", "da32"})
        self.assertEqual(code, 0)
        self.assertIn("Moi key deu co ban ro", out)

    def test_a_key_the_gateway_does_not_know_is_flagged_and_fails(self) -> None:
        """The dangerous direction. Handing this one over looks like a successful
        handover and fails on the team's first call, in their codebase."""
        code, out = _run([_key("da19")], {"da19", "da32"})
        self.assertEqual(code, 1)
        self.assertIn("da32", out)
        self.assertIn("401", out)

    def test_a_key_with_no_plaintext_is_flagged_and_fails(self) -> None:
        """The gateway accepts it; nobody can produce it. `make agent-key` prints nothing
        useful and the cause is not obvious from that alone."""
        code, out = _run([_key("da19"), _key("da32")], {"da19"})
        self.assertEqual(code, 1)
        self.assertIn("KHONG co ban ro", out)

    def test_an_unreadable_secret_does_not_claim_everything_is_fine(self) -> None:
        """No Secret means the handover question cannot be answered. Saying "all good"
        would be the one wrong answer; it still lists what the gateway knows."""
        code, out = _run([_key("da19")], None)
        self.assertEqual(code, 0)
        self.assertIn("CANH BAO", out)
        self.assertNotIn("Moi key deu co ban ro", out)
        self.assertIn("da19", out)


class SpendNoticeTest(unittest.TestCase):
    def test_all_zero_spend_says_it_is_not_being_measured(self) -> None:
        """Zero everywhere means spend tracking is unconfigured, not that nothing was
        used -- and the second reading is the one a budget column invites."""
        _code, out = _run([_key("da19"), _key("da32")], {"da19", "da32"})
        self.assertIn("input_cost_per_token", out)

    def test_real_spend_does_not_trigger_the_notice(self) -> None:
        _code, out = _run([_key("da19", spend=0.42), _key("da32")], {"da19", "da32"})
        self.assertNotIn("input_cost_per_token", out)


class SecretDecodeTest(unittest.TestCase):
    def test_an_empty_value_is_not_counted_as_a_usable_key(self) -> None:
        """A Secret key present but blank would otherwise read as "we can hand this out"
        and export an empty credential."""
        data = json.dumps({"da19": _b64("sk-real"), "da32": _b64("   "), "da39": ""})
        completed = mock.Mock(returncode=0, stdout=data)
        with mock.patch.object(list_keys.subprocess, "run", return_value=completed):
            self.assertEqual(list_keys.secret_aliases("ns", "s"), {"da19"})

    def test_a_missing_secret_returns_none_not_an_empty_set(self) -> None:
        """None means "unknown" and an empty set means "none held" -- conflating them
        turns an unreadable Secret into a false report that no key can be handed out."""
        completed = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(list_keys.subprocess, "run", return_value=completed):
            self.assertIsNone(list_keys.secret_aliases("ns", "s"))


if __name__ == "__main__":
    unittest.main()
