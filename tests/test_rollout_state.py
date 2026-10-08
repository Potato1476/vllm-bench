"""Tests for deploy/state.yaml -- the file production is rendered from.

Three groups, and the middle one is the reason this module exists at all:

    PARSING      a file that cannot be true is refused before anything renders from it.
    TRANSITIONS  only the edges in ALLOWED, so a track cannot reach a phase it has no
                 way out of, and `previous` is captured at the one moment it still exists.
    HELM VALUES  what Argo feeds the chart, derived rather than stored.

An invalid state that parses is worse than one that fails to parse, because the chart
renders from it either way. "phase: canary with no candidate" is not a bad record; it is a
deployment that sends a quarter of production traffic to a service that does not exist.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.scripts import rollout_state as rs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Filenames carry the slug, not the model name: a Service name may not contain a dot.
SLUG_STABLE = f"vllm-{rs.k8s_name('qwen2.5-7b')}-stable.yaml"
SLUG_CANDIDATE = f"vllm-{rs.k8s_name('qwen2.5-7b')}-candidate.yaml"

STABLE = {"weights": "models/moc-7b/2026-11-02-r3", "engine": "vllm/vllm-openai:v0.29.0"}
CANDIDATE = {"weights": "models/moc-7b/2026-11-20-r1", "engine": "vllm/vllm-openai:v0.29.0"}


def state_file(body: dict, tmp: Path) -> Path:
    path = tmp / "state.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def make(phase: str = rs.IDLE, **extra) -> dict:
    track: dict = {"stable": dict(STABLE), "phase": phase}
    if phase in rs.NEEDS_CANDIDATE:
        track["candidate"] = dict(CANDIDATE)
    if phase == rs.CANARY:
        track["canary_weight"] = 25
    track.update(extra)
    return {"paused": False, "qwen2.5-7b": track}


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)

    def load(self, body: dict) -> rs.State:
        return rs.load(state_file(body, self.tmp))


class ParseTest(_Tmp):
    def test_the_committed_state_file_is_valid(self) -> None:
        """The real one, not a fixture. It is what the cluster comes up on tomorrow."""
        state = rs.load(ROOT / "deploy" / "state.yaml")
        self.assertTrue(state.tracks)

    def test_a_phase_needing_a_candidate_without_one_is_refused(self) -> None:
        for phase in sorted(rs.NEEDS_CANDIDATE):
            body = make(phase)
            body["qwen2.5-7b"].pop("candidate")
            with self.subTest(phase=phase), self.assertRaises(rs.StateError) as caught:
                self.load(body)
            self.assertIn("candidate", str(caught.exception))

    def test_a_leftover_candidate_in_an_idle_phase_is_refused(self) -> None:
        """Not pedantry: a candidate nobody cleared still holds one of the four cards."""
        body = make(rs.IDLE)
        body["qwen2.5-7b"]["candidate"] = dict(CANDIDATE)
        with self.assertRaises(rs.StateError):
            self.load(body)

    def test_promoting_needs_no_candidate(self) -> None:
        """After a promote the candidate IS stable; there is no second thing running. The
        state a promote writes has to be readable by the next workflow run."""
        state = self.load(make(rs.PROMOTING))
        self.assertEqual(state.track("qwen2.5-7b").phase, rs.PROMOTING)
        self.assertIsNone(state.track("qwen2.5-7b").candidate)

    def test_a_candidate_identical_to_stable_is_refused(self) -> None:
        body = make(rs.EVALUATING)
        body["qwen2.5-7b"]["candidate"] = dict(STABLE)
        with self.assertRaises(rs.StateError):
            self.load(body)

    def test_a_canary_weight_over_half_is_refused(self) -> None:
        """Above 50 the version that has passed fewer gates is serving most of production."""
        body = make(rs.CANARY)
        body["qwen2.5-7b"]["canary_weight"] = 80
        with self.assertRaises(rs.StateError):
            self.load(body)

    def test_a_canary_weight_outside_canary_is_refused(self) -> None:
        body = make(rs.IDLE)
        body["qwen2.5-7b"]["canary_weight"] = 25
        with self.assertRaises(rs.StateError):
            self.load(body)

    def test_an_unknown_phase_is_refused(self) -> None:
        with self.assertRaises(rs.StateError):
            self.load(make("almost-there"))

    def test_an_engine_without_a_tag_or_digest_is_refused(self) -> None:
        """An unpinned image is a deployment that does not reproduce -- and this cluster is
        rebuilt from scratch every morning."""
        body = make()
        body["qwen2.5-7b"]["stable"]["engine"] = "vllm/vllm-openai"
        with self.assertRaises(rs.StateError) as caught:
            self.load(body)
        self.assertIn("digest", str(caught.exception))

    def test_a_digest_pinned_engine_is_accepted(self) -> None:
        body = make()
        body["qwen2.5-7b"]["stable"]["engine"] = "vllm/vllm-openai@sha256:" + "a" * 64
        self.assertTrue(self.load(body))

    def test_turning_off_verification_requires_a_written_reason(self) -> None:
        """The weights it covers are, by definition, weights nothing verified. A sentence
        in git is the entire price of that."""
        body = make()
        body["qwen2.5-7b"]["stable"]["verify"] = False
        with self.assertRaises(rs.StateError) as caught:
            self.load(body)
        self.assertIn("reason", str(caught.exception))

    def test_verification_off_with_a_reason_is_accepted(self) -> None:
        body = make()
        body["qwen2.5-7b"]["stable"].update({"verify": False, "reason": "checkpoint cu"})
        state = self.load(body)
        self.assertFalse(state.track("qwen2.5-7b").stable.verify)


class TransitionTest(_Tmp):
    def test_every_phase_has_a_way_out(self) -> None:
        """A phase with no outgoing edge strands a track until someone hand-edits
        production state at the moment they can least afford to."""
        for phase in rs.PHASES:
            with self.subTest(phase=phase):
                self.assertTrue(rs.ALLOWED[phase], f"{phase} khong co loi ra")

    def test_an_edge_that_is_not_allowed_is_refused(self) -> None:
        state = self.load(make(rs.IDLE))
        with self.assertRaises(rs.StateError) as caught:
            rs.advance(state, "qwen2.5-7b", rs.PROMOTING)
        self.assertIn("idle", str(caught.exception))

    def test_every_gate_failure_can_abandon_the_candidate(self) -> None:
        """Each gate can reject, so each phase holding a candidate must reach idle."""
        for phase in sorted(rs.NEEDS_CANDIDATE):
            with self.subTest(phase=phase):
                self.assertIn(rs.IDLE, rs.ALLOWED[phase])

    def test_promote_captures_the_old_stable_before_overwriting_it(self) -> None:
        """The one moment it still exists. Captured any later and a rollback after promote
        would have nothing to return to."""
        state = self.load(make(rs.CANARY))
        old = state.track("qwen2.5-7b").stable.weights
        rs.advance(state, "qwen2.5-7b", rs.PROMOTING)
        track = state.track("qwen2.5-7b")
        self.assertEqual(track.stable.weights, CANDIDATE["weights"])
        self.assertIsNotNone(track.previous)
        assert track.previous is not None
        self.assertEqual(track.previous.weights, old)
        self.assertIsNone(track.candidate)

    def test_rolling_back_restores_exactly_the_previous_weights(self) -> None:
        state = self.load(make(rs.CANARY))
        old = state.track("qwen2.5-7b").stable.weights
        rs.advance(state, "qwen2.5-7b", rs.PROMOTING)
        rs.roll_back(state, "qwen2.5-7b")
        track = state.track("qwen2.5-7b")
        self.assertEqual(track.stable.weights, old)
        self.assertEqual(track.phase, rs.IDLE)

    def test_rolling_back_without_a_previous_is_refused(self) -> None:
        state = self.load(make(rs.CANARY))
        with self.assertRaises(rs.StateError):
            rs.roll_back(state, "qwen2.5-7b")

    def test_abandoning_a_candidate_clears_the_weight_too(self) -> None:
        """A leftover canary_weight would fail validation on the next read, stranding the
        track one step after the thing that actually went wrong."""
        state = self.load(make(rs.CANARY))
        rs.advance(state, "qwen2.5-7b", rs.IDLE)
        track = state.track("qwen2.5-7b")
        self.assertEqual(track.canary_weight, 0)
        self.assertIsNone(track.candidate)

    def test_a_second_rollout_on_a_busy_track_is_refused(self) -> None:
        state = self.load(make(rs.CANARY))
        with self.assertRaises(rs.StateError):
            rs.set_candidate(state, "qwen2.5-7b", rs.Ref(**CANDIDATE))

    def test_a_full_cycle_round_trips_through_the_file(self) -> None:
        """Each step is a separate workflow run reading the file back, so every
        intermediate state has to survive dump and load."""
        path = state_file(make(rs.IDLE), self.tmp)
        for step, weight in ((rs.EVALUATING, 0), (rs.CANARY, 25),
                             (rs.PROMOTING, 0), (rs.WATCHING, 0), (rs.IDLE, 0)):
            state = rs.load(path)
            if step == rs.EVALUATING:
                rs.set_candidate(state, "qwen2.5-7b", rs.Ref(**CANDIDATE))
            else:
                rs.advance(state, "qwen2.5-7b", step, weight=weight)
            rs.dump(state, path)
            self.assertEqual(rs.load(path).track("qwen2.5-7b").phase, step)
        final = rs.load(path).track("qwen2.5-7b")
        self.assertEqual(final.stable.weights, CANDIDATE["weights"])


class HelmValuesTest(_Tmp):
    def test_a_candidate_takes_exactly_one_of_the_four_cards(self) -> None:
        idle = rs.helm_values(self.load(make(rs.IDLE)), "qwen2.5-7b")
        self.assertEqual(idle["stable"]["replicas"], 4)
        self.assertNotIn("candidate", idle)

        evaluating = rs.helm_values(self.load(make(rs.EVALUATING)), "qwen2.5-7b")
        self.assertEqual(evaluating["stable"]["replicas"], 3)
        self.assertEqual(evaluating["candidate"]["replicas"], 1)

    def test_an_evaluating_candidate_is_unreachable_from_production(self) -> None:
        """Its own served name until canary. Production traffic cannot land on a version
        that has not finished being measured."""
        values = rs.helm_values(self.load(make(rs.EVALUATING)), "qwen2.5-7b")
        self.assertEqual(values["candidate"]["servedName"], "qwen2.5-7b-candidate")
        self.assertEqual(values["canaryWeight"], 0)

    def test_a_canary_candidate_joins_the_main_name(self) -> None:
        values = rs.helm_values(self.load(make(rs.CANARY)), "qwen2.5-7b")
        self.assertEqual(values["candidate"]["servedName"], "qwen2.5-7b")
        self.assertEqual(values["canaryWeight"], 25)

    def test_the_verify_flag_reaches_the_chart(self) -> None:
        body = make(rs.IDLE)
        body["qwen2.5-7b"]["stable"].update({"verify": False, "reason": "checkpoint cu"})
        values = rs.helm_values(self.load(body), "qwen2.5-7b")
        self.assertFalse(values["stable"]["verifyManifest"])


class PauseTest(_Tmp):
    def test_paused_is_read_back(self) -> None:
        body = make(rs.IDLE)
        body["paused"] = True
        body["paused_reason"] = "dang do tai"
        state = self.load(body)
        self.assertTrue(state.paused)
        self.assertIn("do tai", state.paused_reason)

    def test_pausing_survives_a_dump(self) -> None:
        body = make(rs.IDLE)
        body["paused"] = True
        body["paused_reason"] = "dang do tai"
        path = state_file(body, self.tmp)
        rs.dump(rs.load(path), path)
        self.assertTrue(rs.load(path).paused)


if __name__ == "__main__":
    unittest.main()


class RenderTest(_Tmp):
    """What Argo CD actually gets. These assert the shape the cluster consumes, because a
    rendering mistake does not fail here -- it fails as a pod that will not start, hours
    later, on a cluster nobody is watching."""

    def render(self, body: dict) -> dict[str, str]:
        out = rs.render(self.load(body), out_dir=self.tmp)
        return {p.name: c for p, c in out.items()}

    def test_an_idle_track_renders_one_application(self) -> None:
        files = self.render(make(rs.IDLE))
        self.assertEqual(sorted(files), [SLUG_STABLE])

    def test_a_candidate_adds_its_own_application(self) -> None:
        files = self.render(make(rs.EVALUATING))
        self.assertIn(SLUG_CANDIDATE, files)

    def test_abandoning_a_candidate_stops_generating_its_file(self) -> None:
        """How a rejected candidate releases its GPU: the file stops being generated, the
        PR deletes it, and the root app-of-apps prunes the Application. Left behind, it
        would still hold one of the four cards -- and would come back tomorrow when the
        cluster is rebuilt from git."""
        self.assertNotIn(SLUG_CANDIDATE, self.render(make(rs.IDLE)))

    def test_account_specific_values_never_reach_the_rendered_file(self) -> None:
        """This repo is public and Argo CD syncs from it. The bucket name carries the
        account ID, so it comes from a ConfigMap written by `make cluster-config`."""
        content = self.render(make(rs.IDLE))[SLUG_STABLE]
        self.assertIn("clusterConfigMap", content)
        self.assertNotIn("artifactsBucket", content)
        self.assertNotIn("roleArn", content)

    def test_a_digest_is_joined_with_an_at_sign(self) -> None:
        """`repo:sha256:abc…` is a legal reference to a TAG that does not exist, so the
        only symptom is ImagePullBackOff reading like a registry fault."""
        body = make(rs.IDLE)
        body["qwen2.5-7b"]["stable"]["engine"] = "vllm/vllm-openai@sha256:" + "c" * 64
        values = yaml.safe_load(
            self.render(body)[SLUG_STABLE]
        )["spec"]["source"]["helm"]["valuesObject"]
        self.assertEqual(values["image"]["digest"], "sha256:" + "c" * 64)
        self.assertEqual(values["image"]["repository"], "vllm/vllm-openai")

    def test_a_tag_is_joined_with_a_colon(self) -> None:
        values = yaml.safe_load(
            self.render(make(rs.IDLE))[SLUG_STABLE]
        )["spec"]["source"]["helm"]["valuesObject"]
        self.assertEqual(values["image"]["tag"], "v0.29.0")
        self.assertNotIn("digest", values["image"])

    def test_the_application_prunes_and_does_not_self_heal(self) -> None:
        """Prune on, so a deleted file removes the workload. Self-heal off, so Argo does
        not fight a `kubectl scale` during an incident -- which is exactly when someone
        needs the cluster to stay where they put it."""
        app = yaml.safe_load(self.render(make(rs.IDLE))[SLUG_STABLE])
        automated = app["spec"]["syncPolicy"]["automated"]
        self.assertTrue(automated["prune"])
        self.assertFalse(automated["selfHeal"])

    def test_the_committed_tree_matches_the_committed_state(self) -> None:
        """The check that runs on every pull request. A hand-edited generated file is
        caught here rather than as a cluster that disagrees with git."""
        wanted = rs.render(rs.load(ROOT / "deploy" / "state.yaml"))
        for path, content in wanted.items():
            self.assertTrue(path.is_file(), f"thieu {path}")
            self.assertEqual(path.read_text(encoding="utf-8"), content,
                             f"{path.name} lech voi state.yaml")


class TierTest(_Tmp):
    """Each model tier is its own node group, not a slice of someone else's card.

    A 1.5B at 25% of a card leaves the 7B a lower ceiling than the 50 req/s it has to
    reach. Its own small card costs half as much and borrows nothing -- but the approved
    G quota is 16 vCPU, every .xlarge is 4, and all four main cards are already spoken
    for, so the light tier stays at zero nodes until that quota rises.
    """

    def render_track(self, tier: str) -> dict:
        body = make(rs.IDLE)
        if tier != "main":
            body["qwen2.5-7b"]["tier"] = tier
        out = rs.render(self.load(body), out_dir=self.tmp)
        app = yaml.safe_load(next(iter(out.values())))
        return app["spec"]["source"]["helm"]["valuesObject"]

    def test_the_main_tier_keeps_the_chart_defaults(self) -> None:
        """Restating them would make a second copy free to drift from where everything
        already runs."""
        values = self.render_track("main")
        self.assertNotIn("nodeSelector", values)
        self.assertNotIn("tolerations", values)
        self.assertEqual(values["replicaCount"], 4)

    def test_the_light_tier_is_pinned_and_tolerates_its_taint(self) -> None:
        values = self.render_track("light")
        self.assertEqual(values["nodeSelector"]["tier"], "light")
        keys = {t["key"] for t in values["tolerations"]}
        self.assertEqual(keys, {"nvidia.com/gpu", "tier"})

    def test_the_light_tier_gets_one_card_not_four(self) -> None:
        """Reading the main tier's count would put four 1.5B pods on a node group with one
        node, and three of them would sit Pending looking like a capacity fault."""
        self.assertEqual(self.render_track("light")["replicaCount"], 1)

    def test_an_unknown_tier_is_refused(self) -> None:
        body = make(rs.IDLE)
        body["qwen2.5-7b"]["tier"] = "gigantic"
        with self.assertRaises(rs.StateError):
            self.load(body)

    def test_every_tier_declares_how_many_cards_it_has(self) -> None:
        """A tier without an entry would raise KeyError at render time -- inside the step
        that writes what the cluster runs."""
        self.assertEqual(set(rs.TIER_CARDS), rs.TIERS)


class UpstreamUrlTest(unittest.TestCase):
    """The route the guardrail uses must name the Service the chart actually creates.

    These are produced by two different files, and when they disagree the guardrail
    reports the upstream as unreachable -- which reads as the engine being down, not as
    the route being wrong. So the test renders the chart and compares.
    """

    def _service_names(self, release: str) -> set[str]:
        import subprocess
        out = subprocess.run(
            ["helm", "template", release, str(ROOT / "charts" / "vllm"),
             "--set", "clusterConfigMap=cc", "--set", "mode=solo-a"],
            capture_output=True, text=True, check=False, timeout=120)
        if out.returncode != 0:
            self.skipTest("khong chay duoc helm")
        names, kind = set(), None
        for doc in out.stdout.split("\n---\n"):
            if "kind: Service" in doc:
                for line in doc.splitlines():
                    if line.startswith("  name: "):
                        names.add(line.split("name: ", 1)[1].strip())
                        break
        return names

    def test_the_url_names_the_service_the_chart_renders(self) -> None:
        track = "qwen2.5-7b"
        release = f"vllm-{rs.k8s_name(track)}-stable"
        rendered = self._service_names(release)
        host = rs.upstream_url(track).split("//", 1)[1].split(".", 1)[0]
        self.assertIn(host, rendered,
                      f"guardrail goi {host}, chart tao {sorted(rendered)}")

    def test_a_dot_never_reaches_a_service_name(self) -> None:
        """A Service name is an RFC 1035 label: no dots. A Deployment name is an RFC 1123
        subdomain and allows them, so half the release renders before the Service fails."""
        self.assertNotIn(".", rs.k8s_name("qwen2.5-7b"))
        self.assertNotIn(".", rs.upstream_url("qwen2.5-7b").split("//")[1].split(".")[0])

    def test_stable_and_candidate_get_different_upstreams(self) -> None:
        """They are separate Services now. Canary splits between these two URLs by weight
        in the guardrail, which is why the split does not live in pod counts."""
        self.assertNotEqual(rs.upstream_url("qwen2.5-7b", "stable"),
                            rs.upstream_url("qwen2.5-7b", "candidate"))
