"""Tests for the simulated consumers.

These exist because a simulation is easy to make vacuous without noticing. Seven copies of
the same request, or seven workloads that all take the grounded path, would still print a
tidy table and would prove nothing beyond "the gateway can count to seven". The checks
below pin the properties that make a run worth reading.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bench.agents_sim.workloads import WORKLOADS, by_id

ROOT = Path(__file__).resolve().parents[1]


class RosterTest(unittest.TestCase):
    def test_one_workload_per_project_named_in_the_brief(self) -> None:
        self.assertEqual({w.agent_id for w in WORKLOADS},
                         {f"da{n}" for n in (19, 20, 32, 39, 41, 44, 45)})

    def test_every_workload_has_a_key_provisioned_for_it(self) -> None:
        """A workload with no entry in agents.json gets no key and dies at auth."""
        agents = json.loads((ROOT / "bench" / "agents.json").read_text(encoding="utf-8"))
        configured = {a["id"]: a for a in agents["agents"]}
        for w in WORKLOADS:
            with self.subTest(agent=w.agent_id):
                self.assertIn(w.agent_id, configured)
                self.assertIn(w.model, configured[w.agent_id]["models"],
                              f"{w.agent_id} goi {w.model} nhung key khong duoc cap")


class CoverageTest(unittest.TestCase):
    """The simulation has to span the platform, not repeat one corner of it."""

    def test_both_profiles_are_exercised(self) -> None:
        grounded = [w for w in WORKLOADS if w.grounded]
        plain = [w for w in WORKLOADS if not w.grounded]
        self.assertGreaterEqual(len(grounded), 2, "khong du duong co trich dan")
        self.assertGreaterEqual(len(plain), 2, "khong du duong khong-RAG")

    def test_both_model_tiers_are_exercised(self) -> None:
        self.assertTrue(any("7b" in w.model for w in WORKLOADS))
        self.assertTrue(any("1.5b" in w.model for w in WORKLOADS))

    def test_output_budgets_actually_differ(self) -> None:
        """A classifier emitting two words and a summariser emitting a paragraph put very
        different pressure on the engine. Identical max_tokens would mean the spread is
        cosmetic."""
        self.assertGreaterEqual(len({w.max_tokens for w in WORKLOADS}), 3)

    def test_input_sizes_actually_differ(self) -> None:
        sizes = [max(len(i) for i in w.inputs) for w in WORKLOADS]
        self.assertGreater(max(sizes), 3 * min(sizes),
                           "moi workload gui input dai gan bang nhau")

    def test_adversarial_samples_reach_both_profiles(self) -> None:
        """The guardrail must be exercised where it is easiest to lose it. The plain
        profile drops retrieval and citations, so proving injection screening still fires
        THERE is the point -- a clean-traffic run cannot tell it from switched off."""
        with_attacks = [w for w in WORKLOADS if w.adversarial]
        self.assertGreaterEqual(len(with_attacks), 3)
        self.assertTrue(any(w.grounded for w in with_attacks))
        self.assertTrue(any(not w.grounded for w in with_attacks))

    def test_expected_refusal_stages_are_real_stages(self) -> None:
        """A typo here makes a blocked attack look like a miss, which reports a working
        guardrail as broken."""
        known = {"injection", "pii_ingress", "retrieval", "policy", "known_answer",
                 "grounding", "pii_egress", "invalid_request"}
        for w in WORKLOADS:
            for text, stage in w.adversarial:
                with self.subTest(agent=w.agent_id, stage=stage):
                    self.assertIn(stage, known)
                    self.assertTrue(text.strip())


class PlainProfileTest(unittest.TestCase):
    def test_plain_workloads_name_a_plain_model(self) -> None:
        """The profile rides on the model name; a plain workload asking for the grounded
        name would be refused at grounding on every request, which is the exact failure
        this whole profile exists to remove."""
        for w in WORKLOADS:
            if not w.grounded:
                with self.subTest(agent=w.agent_id):
                    self.assertTrue(w.model.endswith("-plain"))

    def test_the_analyst_pilot_key_is_not_simulated_here(self) -> None:
        """moc-da-pilot backs a human chat UI and is deliberately denied the plain
        profile. Driving it from a load simulation would mix synthetic traffic into the
        series the pilot's own measurements read."""
        self.assertNotIn("moc-da-pilot", {w.agent_id for w in WORKLOADS})


class LookupTest(unittest.TestCase):
    def test_by_id_round_trips(self) -> None:
        self.assertEqual(by_id("da32").agent_id, "da32")

    def test_an_unknown_id_raises_rather_than_returning_none(self) -> None:
        with self.assertRaises(KeyError):
            by_id("da99")


if __name__ == "__main__":
    unittest.main()
