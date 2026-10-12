"""The rollout controller: one step per run, decided from S3, written to the state file."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from bench.scripts import rollout_controller as rc
from bench.scripts import rollout_state as rs

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
STABLE = {"weights": "models/qwen2.5-7b/v1", "engine": "vllm/vllm-openai:v0.29.0"}
CATALOG = {"qwen2.5-7b": rc.Watch("qwen2.5-7b", "models/qwen2.5-7b/", ("external",),
                                  True, 25)}


class FakeStore:
    def __init__(self) -> None:
        self.versions: list[rc.Version] = []
        self.reports: dict[str, dict] = {}

    def publish(self, version: str, at: datetime) -> str:
        prefix = f"models/qwen2.5-7b/{version}"
        self.versions.append(rc.Version(prefix, at))
        return prefix

    def list_ready(self, watch: str) -> list[rc.Version]:
        return [v for v in self.versions if v.prefix.startswith(watch)]

    def read_json(self, key: str) -> dict | None:
        return self.reports.get(key)


def passing_gate(prefix: str, entry: rc.Watch) -> rc.GateResult:
    return True, [], {"signer": "external", "weights_gib": 5.2, "served_name": "qwen2.5-7b"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "state.yaml"
        self.path.write_text(yaml.safe_dump(
            {"paused": False, "qwen2.5-7b": {"stable": dict(STABLE), "phase": "idle"}}))
        self.store = FakeStore()
        self.gate = passing_gate

    def run_step(self, now: datetime) -> rc.Step:
        """Load, step, dump -- every real step is a separate workflow run on a fresh
        checkout, so state that only survives in memory is state that does not exist."""
        state = rs.load(self.path)
        result = rc.step(state, CATALOG, self.store, self.gate, now)
        rs.dump(state, self.path)
        return result

    @property
    def track(self) -> rs.Track:
        return rs.load(self.path).track("qwen2.5-7b")

    def report(self, kind: str, ok: bool, ref: dict | rs.Ref | None = None,
               reasons: list[str] | None = None) -> None:
        track = self.track
        target = ref if isinstance(ref, rs.Ref) else (track.candidate or track.stable)
        rid = rs.rollout_id("qwen2.5-7b", target)
        self.store.reports[rs.report_key(rid, kind)] = {"ok": ok, "reasons": reasons or []}


class ProposeTest(_Base):
    def test_a_new_version_becomes_the_candidate(self) -> None:
        prefix = self.store.publish("v2", T0)
        result = self.run_step(T0 + timedelta(minutes=1))
        self.assertEqual(self.track.phase, rs.EVALUATING)
        self.assertEqual(self.track.candidate.weights, prefix)
        self.assertEqual(self.track.candidate.weights_gib, 5.2)
        self.assertTrue(self.track.candidate.verify)
        self.assertEqual(self.track.watermark, "2026-10-12T09:00:00Z")
        self.assertEqual([e.kind for e in result.events], ["candidate"])

    def test_the_candidate_keeps_the_stable_engine(self) -> None:
        """A model rollout changes the weights only. Changing both at once would leave a
        failed evaluation unable to say which of the two caused it."""
        self.store.publish("v2", T0)
        self.run_step(T0)
        self.assertEqual(self.track.candidate.engine, STABLE["engine"])

    def test_of_two_new_versions_the_newest_wins_and_the_older_is_never_proposed(self) -> None:
        self.store.publish("v2", T0)
        newest = self.store.publish("v3", T0 + timedelta(minutes=5))
        result = self.run_step(T0 + timedelta(minutes=6))
        self.assertEqual(self.track.candidate.weights, newest)
        self.assertIn("skipped", [e.kind for e in result.events])
        self.assertEqual(self.track.watermark, "2026-10-12T09:05:00Z")

    def test_a_failing_gate_rejects_without_deploying_anything(self) -> None:
        self.gate = lambda prefix, entry: (False, ["[VALID] khong co _READY"], {})
        prefix = self.store.publish("v2", T0)
        result = self.run_step(T0)
        self.assertEqual(self.track.phase, rs.IDLE)
        self.assertIsNone(self.track.candidate)
        self.assertEqual(self.track.rejected[-1]["weights"], prefix)
        self.assertEqual(result.events[-1].issue, "open")
        # ...and stays rejected on every later run.
        self.gate = passing_gate
        self.assertFalse(self.run_step(T0 + timedelta(hours=1)).events)

    def test_a_version_published_for_another_served_name_is_refused(self) -> None:
        self.gate = lambda p, e: (True, [], {"served_name": "qwen2.5-1.5b"})
        self.store.publish("v2", T0)
        self.run_step(T0)
        self.assertEqual(self.track.phase, rs.IDLE)
        self.assertIn("served_name", self.track.rejected[-1]["reason"])

    def test_nothing_happens_while_paused(self) -> None:
        state = rs.load(self.path)
        state.paused, state.paused_reason = True, "do tai TC1a"
        rs.dump(state, self.path)
        self.store.publish("v2", T0)
        result = self.run_step(T0)
        self.assertFalse(result.changed)
        self.assertEqual(self.track.phase, rs.IDLE)

    def test_auto_rollout_off_proposes_nothing(self) -> None:
        global CATALOG
        saved = CATALOG
        CATALOG = {"qwen2.5-7b": rc.Watch("qwen2.5-7b", "models/qwen2.5-7b/",
                                          ("external",), False, 25)}
        try:
            self.store.publish("v2", T0)
            self.assertFalse(self.run_step(T0).changed)
        finally:
            CATALOG = saved


class LifecycleTest(_Base):
    def test_a_full_rollout(self) -> None:
        new = self.store.publish("v2", T0)
        self.run_step(T0)

        # No report yet: wait, silently, however long the cluster is off.
        self.assertFalse(self.run_step(T0 + timedelta(hours=1)).changed)

        self.report("eval", True)
        self.run_step(T0 + timedelta(hours=1, minutes=15))
        self.assertEqual((self.track.phase, self.track.canary_weight), (rs.CANARY, 25))

        self.report("canary", True)
        promoted = self.run_step(T0 + timedelta(hours=1, minutes=45))
        self.assertEqual(self.track.phase, rs.PROMOTING)
        self.assertEqual(self.track.stable.weights, new)
        self.assertEqual(self.track.previous.weights, STABLE["weights"])
        self.assertEqual(promoted.events[-1].issue, "open")

        # The roll takes time and nothing reports when it ends.
        self.assertFalse(self.run_step(T0 + timedelta(hours=1, minutes=50)).changed)
        self.run_step(T0 + timedelta(hours=2, minutes=15))
        self.assertEqual(self.track.phase, rs.WATCHING)

        self.report("watch", True)
        done = self.run_step(T0 + timedelta(hours=3, minutes=30))
        self.assertEqual(self.track.phase, rs.IDLE)
        self.assertEqual(self.track.stable.weights, new)
        self.assertEqual(done.events[-1].issue, "close")
        # Nothing new published: the finished rollout does not start again.
        self.assertFalse(self.run_step(T0 + timedelta(hours=4)).changed)

    def test_a_failed_evaluation_rejects_with_the_jobs_reason(self) -> None:
        self.store.publish("v2", T0)
        self.run_step(T0)
        self.report("eval", False, reasons=["trich dan 120/144 < 95%"])
        result = self.run_step(T0 + timedelta(hours=1))
        self.assertEqual(self.track.phase, rs.IDLE)
        self.assertIn("trich dan", self.track.rejected[-1]["reason"])
        self.assertEqual(result.events[-1].kind, "rejected")

    def test_a_failed_canary_puts_all_traffic_back_on_stable(self) -> None:
        self.store.publish("v2", T0)
        self.run_step(T0)
        self.report("eval", True)
        self.run_step(T0 + timedelta(hours=1))
        self.report("canary", False, reasons=["p95"])
        self.run_step(T0 + timedelta(hours=2))
        state = rs.load(self.path)
        self.assertEqual(rs.route_table(state)["qwen2.5-7b"], rs.upstream_url("qwen2.5-7b"))
        self.assertEqual(state.track("qwen2.5-7b").stable.weights, STABLE["weights"])

    def test_a_failed_watch_rolls_back_and_the_version_never_returns(self) -> None:
        new = self.store.publish("v2", T0)
        self.run_step(T0)
        self.report("eval", True)
        self.run_step(T0 + timedelta(hours=1))
        self.report("canary", True)
        self.run_step(T0 + timedelta(hours=2))
        self.run_step(T0 + timedelta(hours=3))
        self.report("watch", False, reasons=["availability 98.1%"])
        result = self.run_step(T0 + timedelta(hours=4))
        self.assertEqual(result.events[-1].kind, "rolled_back")
        self.assertEqual(self.track.phase, rs.IDLE)
        self.assertEqual(self.track.stable.weights, STABLE["weights"])
        self.assertEqual(self.track.rejected[-1]["weights"], new)
        self.assertFalse(self.run_step(T0 + timedelta(hours=5)).changed)

    def test_a_long_silence_is_flagged_not_acted_on(self) -> None:
        self.store.publish("v2", T0)
        self.run_step(T0)
        result = self.run_step(T0 + timedelta(hours=7))
        self.assertEqual([e.kind for e in result.events], ["stalled"])
        self.assertFalse(result.changed)
        self.assertEqual(self.track.phase, rs.EVALUATING)

    def test_every_step_renders_a_tree_the_pr_check_accepts(self) -> None:
        """The controller writes state AND the rendered files; the PR check then
        re-renders and compares. A step whose output fails that check never merges."""
        out = Path(self._dir.name) / "deploy"
        self.store.publish("v2", T0)
        for when, report in ((T0, None), (T0 + timedelta(hours=1), "eval"),
                             (T0 + timedelta(hours=2), "canary")):
            if report:
                self.report(report, True)
            self.run_step(when)
            state = rs.load(self.path)
            rs.write_rendered(state, out)
            wanted, stale = rs.plan_render(state, out)
            self.assertEqual(stale, [])
            for path, content in wanted.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)


class CatalogTest(unittest.TestCase):
    def write(self, body: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "catalog.yaml"
        path.write_text(yaml.safe_dump(body))
        return path

    def test_the_committed_catalog_matches_the_committed_state(self) -> None:
        catalog = rc.load_catalog(state=rs.load())
        self.assertEqual(set(catalog), set(rs.load().tracks))

    def test_refusals(self) -> None:
        ok = {"watch": "models/x/", "allowed_signers": ["external"]}
        bad = {
            "watch too broad": {**ok, "watch": "models/"},
            "watch too deep": {**ok, "watch": "models/x/y/"},
            "no signers": {**ok, "allowed_signers": []},
            "unknown signer": {**ok, "allowed_signers": ["nobody"]},
            "weight above 50": {**ok, "canary_weight": 60},
        }
        for label, body in bad.items():
            with self.subTest(label), self.assertRaises(rc.CatalogError):
                rc.load_catalog(self.write({"x": body}))

    def test_two_tracks_cannot_watch_one_prefix(self) -> None:
        entry = {"watch": "models/x/", "allowed_signers": ["external"]}
        with self.assertRaises(rc.CatalogError):
            rc.load_catalog(self.write({"a": entry, "b": entry}))


class S3StoreTest(unittest.TestCase):
    def test_only_ready_files_one_level_down_are_versions(self) -> None:
        listing = {"Contents": [
            {"Key": "models/qwen2.5-7b/v1/_READY", "LastModified": "2026-10-12T09:00:00.000Z"},
            {"Key": "models/qwen2.5-7b/v1/model.safetensors", "LastModified": "2026-10-12T08:00:00+00:00"},
            {"Key": "models/qwen2.5-7b/v2/sub/_READY", "LastModified": "2026-10-12T09:00:00+00:00"},
            {"Key": "models/qwen2.5-7b/_READY", "LastModified": "2026-10-12T09:00:00+00:00"},
        ]}
        done = subprocess.CompletedProcess([], 0, json.dumps(listing), "")
        with mock.patch.object(rc, "_aws", return_value=done):
            versions = rc.S3Store("b").list_ready("models/qwen2.5-7b/")
        self.assertEqual(versions, [rc.Version("models/qwen2.5-7b/v1", T0)])

    def test_a_missing_report_is_none_and_any_other_failure_is_loud(self) -> None:
        missing = subprocess.CompletedProcess([], 1, "", "An error occurred (404) when calling the HeadObject operation: Not Found")
        denied = subprocess.CompletedProcess([], 1, "", "An error occurred (403) Forbidden")
        with mock.patch.object(rc, "_aws", return_value=missing):
            self.assertIsNone(rc.S3Store("b").read_json("runs/rollout/x/eval.json"))
        # Access denied must not read as "no report yet": that would wait forever on a
        # permissions problem instead of failing the run where someone sees it.
        with mock.patch.object(rc, "_aws", return_value=denied), \
                self.assertRaises(RuntimeError):
            rc.S3Store("b").read_json("runs/rollout/x/eval.json")


if __name__ == "__main__":
    unittest.main()
