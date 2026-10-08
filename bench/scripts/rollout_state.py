#!/usr/bin/env python3
"""deploy/state.yaml -- what production is meant to be running, and how far a rollout got.

    rollout_state.py validate                       # the PR check
    rollout_state.py show
    rollout_state.py helm-values --track qwen2.5-7b # what Argo feeds the chart
    rollout_state.py advance --track … --to canary --weight 25

WHY THE STATE IS IN GIT AND NOT IN THE CLUSTER

The cluster is destroyed every evening. Anything a rollout controller remembered in
cluster state -- that this candidate was rejected, that the roll was halfway through --
dies with it, and the cluster that comes up tomorrow would happily promote the version
that was thrown out yesterday. A commit survives that. It is also readable, revertable,
and leaves the rollout history in a place that does not require the cluster to be up.

Argo CD syncs the cluster TO this file. Nothing in the cluster writes back to it; only
rollout.yml does, through a pull request.

THE PHASES

    idle        stable is serving. No candidate.
    evaluating  candidate exists, has its own served name, takes no production traffic.
    canary      candidate is in the main pool at canary_weight percent.
    promoting   candidate became stable; pods are rolling.
    watching    the roll finished; still inside the window where it can be undone.

Only `advance` moves between them, and only along the edges in ALLOWED. A state file that
reaches an impossible phase is worse than one that fails to parse: the chart renders from
it, so "candidate: null while phase: canary" is a deployment that routes a quarter of
production traffic to nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "deploy" / "state.yaml"

IDLE, EVALUATING, CANARY, PROMOTING, WATCHING = (
    "idle", "evaluating", "canary", "promoting", "watching")
PHASES = (IDLE, EVALUATING, CANARY, PROMOTING, WATCHING)

# Every edge a rollout may take, including the ones that go backwards. A transition
# missing here is refused rather than assumed: the backwards edges are how a failed gate
# abandons a candidate, and leaving one out would strand a track in a phase it cannot
# leave without someone hand-editing production state.
ALLOWED: dict[str, set[str]] = {
    IDLE:       {EVALUATING},
    EVALUATING: {CANARY, IDLE},          # IDLE = candidate rejected
    CANARY:     {PROMOTING, IDLE},       # IDLE = candidate rejected
    PROMOTING:  {WATCHING, IDLE},        # IDLE = rolled back during the roll
    WATCHING:   {IDLE},                  # IDLE either way; `previous` says which
}

# Phases during which a separate candidate pod exists. PROMOTING is deliberately NOT one
# of them: at that point the candidate has BECOME stable and the four pods are rolling
# onto it, so there is no second thing to run. Requiring a candidate here made the state
# written by a promote impossible to read back -- caught by round-tripping a full cycle
# through the file rather than by exercising the transitions in memory, which is how a
# rollout actually runs: every step is a separate workflow run that loads the file again.
NEEDS_CANDIDATE = {EVALUATING, CANARY}

# Node tiers, each a separate EKS node group. Adding one here means adding a node group
# in terraform/cluster/eks.tf with a matching `tier` label and taint.
TIERS = {"main", "light"}


def k8s_name(track: str) -> str:
    """A track name Kubernetes will accept as part of a Service name.

    Model names carry dots -- qwen2.5-7b -- and a Service name may not: it has to be an
    RFC 1035 label, letters digits and hyphens only. Deployment names are RFC 1123
    subdomains and DO allow dots, so the mistake renders half a release successfully and
    then fails on the Service, which reads as a cluster problem rather than a naming one.
    """
    return track.replace(".", "-")

# How many cards each tier has. Main is four because the approved G quota is 16 vCPU and
# every .xlarge is four of them; light is one and stays one until that quota rises, since
# the four main cards are exactly what 7B needs for 50 req/s at 12.5 req/s per card.
TIER_CARDS = {"main": 4, "light": 1}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")

# Two shapes, validated apart. They are both "a string naming a thing" and it is tempting
# to check them with one pattern, but the mistakes they admit are different: a weights
# path with a colon in it is a typo, while an image without one is an unpinned `latest`.
_WEIGHTS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,255}$")
_ENGINE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,200}"
    r"(:[A-Za-z0-9][A-Za-z0-9._\-]{0,63}|@sha256:[0-9a-f]{64})$")


class StateError(ValueError):
    """The state file says something that cannot be true."""


@dataclass(frozen=True)
class Ref:
    """One deployable thing: a weights version plus the engine image that serves it."""
    weights: str
    engine: str
    verify: bool = True
    reason: str = ""
    # Measured by check_model.py from the files themselves, carried here so rendering
    # needs no network. charts/vllm declares weightsGiB by hand, which is right for the
    # checkpoints a person put there and silently wrong for any version that arrived on
    # its own: the chart's fit check would then be comparing against a figure describing
    # some other model. Absent means "fall back to the chart", correct only for the
    # hand-published checkpoints the chart was written around.
    weights_gib: float | None = None

    @staticmethod
    def parse(raw: object, where: str) -> "Ref":
        if not isinstance(raw, dict):
            raise StateError(f"{where}: phai la mot anh xa, nhan duoc {type(raw).__name__}")
        weights, engine = raw.get("weights"), raw.get("engine")
        if not isinstance(weights, str) or not _WEIGHTS_RE.match(weights):
            raise StateError(f"{where}.weights khong hop le: {weights!r} "
                             "(mong doi mot prefix S3, vd models/<ten>/<version>)")
        if not isinstance(engine, str) or not _ENGINE_RE.match(engine):
            raise StateError(
                f"{where}.engine khong hop le: {engine!r}. Phai co tag hoac digest -- mot "
                "image khong ghim la mot ban trien khai khong lap lai duoc, va khi dung "
                "lai cum moi sang thi co the da la image khac.")
        verify = raw.get("verify", True)
        if not isinstance(verify, bool):
            raise StateError(f"{where}.verify phai la true/false")
        reason = raw.get("reason", "")
        # An exception with no stated reason is one nobody can review later. The weights
        # these cover are, by definition, weights nothing verified -- so the cost of
        # writing a sentence is the whole price of turning the check off.
        if not verify and not reason:
            raise StateError(
                f"{where}: verify=false phai kem reason. Tat kiem manifest nghia la nap "
                "trong so khong ai ky; ly do phai doc duoc trong git.")
        if not isinstance(reason, str):
            raise StateError(f"{where}.reason phai la chuoi")
        weights_gib = raw.get("weights_gib")
        if weights_gib is not None and not isinstance(weights_gib, (int, float)):
            raise StateError(f"{where}.weights_gib phai la so")
        return Ref(weights, engine, verify, reason,
                   float(weights_gib) if weights_gib is not None else None)

    def to_dict(self) -> dict:
        out: dict[str, object] = {"weights": self.weights, "engine": self.engine}
        if not self.verify:
            out["verify"] = False
            out["reason"] = self.reason
        if self.weights_gib is not None:
            out["weights_gib"] = self.weights_gib
        return out


@dataclass
class Track:
    """One served model name and everything about its rollout."""
    name: str
    stable: Ref
    phase: str = IDLE
    candidate: Ref | None = None
    previous: Ref | None = None
    canary_weight: int = 0
    candidate_since: str = ""
    # Which GPU tier this model belongs on. "main" is the measurement fleet; "light" is
    # the small, cheap card for models that do not need a big one.
    #
    # A 1.5B taking a slice of a card the 7B needs lowers the ceiling of the model that
    # has to reach 50 req/s, which is why `mode: shared` is the wrong shape for it. Its
    # own node is both cheaper and does not borrow from anything.
    tier: str = "main"

    def to_dict(self) -> dict:
        out: dict[str, object] = {"stable": self.stable.to_dict(), "phase": self.phase}
        if self.candidate:
            out["candidate"] = self.candidate.to_dict()
        if self.previous:
            out["previous"] = self.previous.to_dict()
        if self.phase == CANARY:
            out["canary_weight"] = self.canary_weight
        if self.candidate_since:
            out["candidate_since"] = self.candidate_since
        if self.tier != "main":
            out["tier"] = self.tier
        return out


def _parse_track(name: str, raw: object) -> Track:
    if not isinstance(raw, dict):
        raise StateError(f"{name}: phai la mot anh xa")
    if not _NAME_RE.match(name):
        raise StateError(f"ten track khong hop le: {name!r}")
    if "stable" not in raw:
        raise StateError(f"{name}: thieu stable -- khong biet dang phuc vu gi")

    phase = raw.get("phase", IDLE)
    if phase not in PHASES:
        raise StateError(f"{name}.phase khong hop le: {phase!r} (cho phep: {', '.join(PHASES)})")

    track = Track(
        name=name,
        stable=Ref.parse(raw["stable"], f"{name}.stable"),
        phase=phase,
        candidate=Ref.parse(raw["candidate"], f"{name}.candidate") if raw.get("candidate") else None,
        previous=Ref.parse(raw["previous"], f"{name}.previous") if raw.get("previous") else None,
        canary_weight=int(raw.get("canary_weight") or 0),
        candidate_since=str(raw.get("candidate_since") or ""),
        tier=str(raw.get("tier") or "main"),
    )
    if track.tier not in TIERS:
        raise StateError(f"{name}.tier khong hop le: {track.tier!r} "
                         f"(cho phep: {', '.join(sorted(TIERS))})")

    # --- the invariants the chart renders from -------------------------------------
    if track.phase in NEEDS_CANDIDATE and track.candidate is None:
        raise StateError(
            f"{name}: phase={track.phase} nhung khong co candidate. Chart render tu file "
            "nay, nen trang thai nay la mot ban trien khai dinh tuyen traffic vao hu khong.")
    if track.phase not in NEEDS_CANDIDATE and track.candidate is not None:
        raise StateError(
            f"{name}: phase={track.phase} ma van con candidate. Mot ung vien bi bo quen o "
            "day van chiem mot GPU trong bon card.")
    if track.phase == CANARY:
        if not 1 <= track.canary_weight <= 50:
            raise StateError(
                f"{name}.canary_weight = {track.canary_weight}; phai trong 1..50. Tren 50 "
                "thi ung vien chua duoc kiem da nhan phan lon traffic.")
    elif track.canary_weight:
        raise StateError(f"{name}: canary_weight chi co nghia o phase canary")
    if track.candidate and track.candidate.weights == track.stable.weights \
            and track.candidate.engine == track.stable.engine:
        raise StateError(f"{name}: candidate trung het voi stable -- khong co gi de rollout")
    return track


@dataclass
class State:
    tracks: dict[str, Track]
    paused: bool = False
    paused_reason: str = ""

    def track(self, name: str) -> Track:
        if name not in self.tracks:
            raise StateError(f"khong co track {name!r} (co: {', '.join(sorted(self.tracks))})")
        return self.tracks[name]

    def to_dict(self) -> dict:
        out: dict[str, object] = {"paused": self.paused}
        if self.paused_reason:
            out["paused_reason"] = self.paused_reason
        for name in sorted(self.tracks):
            out[name] = self.tracks[name].to_dict()
        return out


def load(path: Path = STATE_PATH) -> State:
    if not path.is_file():
        raise StateError(f"khong co {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise StateError(f"{path}: goc phai la mot anh xa")
    paused = raw.pop("paused", False)
    if not isinstance(paused, bool):
        raise StateError("paused phai la true/false")
    reason = raw.pop("paused_reason", "")
    tracks = {name: _parse_track(name, body) for name, body in raw.items()}
    if not tracks:
        raise StateError("khong co track nao")
    return State(tracks, bool(paused), str(reason or ""))


def dump(state: State, path: Path = STATE_PATH) -> None:
    header = (
        "# Trang thai trien khai. CHI rollout.yml sua file nay, qua pull request.\n"
        "# Kiem bang: python3 bench/scripts/rollout_state.py validate\n"
        "#\n"
        "# paused: true dung MOI rollout o buoc ke tiep. Bat truoc khi do tai -- mot lan\n"
        "# rollout chen vao giua phep do lam cum chi con 3/4 dung luong va so TC1a sai.\n"
    )
    path.write_text(header + yaml.safe_dump(state.to_dict(), sort_keys=False,
                                            allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")


# ------------------------------------------------------------------------- transitions


def advance(state: State, name: str, to: str, *, weight: int = 0, now: str = "") -> State:
    """Move one track to `to`, refusing any edge not in ALLOWED."""
    track = state.track(name)
    if to not in PHASES:
        raise StateError(f"phase khong hop le: {to!r}")
    if to not in ALLOWED[track.phase]:
        raise StateError(
            f"{name}: khong di tu {track.phase} sang {to} duoc "
            f"(cho phep: {', '.join(sorted(ALLOWED[track.phase])) or 'khong gi'})")

    if to == CANARY:
        track.canary_weight = weight or 25
    elif to == PROMOTING:
        # `previous` is captured HERE, at the only moment the old stable is still known.
        # Taken any later it would already have been overwritten, and rollback after a
        # promote would have nothing to return to.
        assert track.candidate is not None
        track.previous = track.stable
        track.stable = track.candidate
        track.candidate = None
        track.canary_weight = 0
    elif to == IDLE:
        track.candidate = None
        track.canary_weight = 0
        track.candidate_since = ""
    track.phase = to
    if to == EVALUATING and now:
        track.candidate_since = now
    return state


def roll_back(state: State, name: str) -> State:
    """Undo a promote. Only meaningful while `previous` still holds the old stable."""
    track = state.track(name)
    if track.phase not in (PROMOTING, WATCHING):
        raise StateError(f"{name}: chi quay ve duoc o phase promoting hoac watching")
    if track.previous is None:
        raise StateError(f"{name}: khong co previous de quay ve")
    track.stable, track.previous = track.previous, None
    track.candidate = None
    track.canary_weight = 0
    track.phase = IDLE
    return state


def set_candidate(state: State, name: str, candidate: Ref, *, now: str = "") -> State:
    track = state.track(name)
    if track.phase != IDLE:
        raise StateError(
            f"{name}: dang o phase {track.phase}; mot track chi chay mot rollout mot luc")
    track.candidate = candidate
    track.phase = EVALUATING
    track.candidate_since = now
    return state


# ------------------------------------------------------------------------ helm values


def helm_values(state: State, name: str) -> dict:
    """What Argo hands the vLLM chart for this track.

    Derived rather than stored: a second copy of "how many stable pods are there" would
    disagree with the phase sooner or later, and the disagreement shows up as a card that
    is either idle or oversubscribed.
    """
    track = state.track(name)
    candidate = track.candidate
    # Four cards on the main tier; the light tier has one. A candidate takes exactly one,
    # which is why evaluation runs at a twelfth of production load rather than at fifty.
    stable_replicas = TIER_CARDS[track.tier] - (1 if candidate else 0)
    values: dict[str, object] = {
        "servedName": name,
        "stable": {
            "weights": track.stable.weights,
            "engine": track.stable.engine,
            "replicas": stable_replicas,
            "verifyManifest": track.stable.verify,
        },
        "phase": track.phase,
    }
    if candidate:
        values["candidate"] = {
            "weights": candidate.weights,
            "engine": candidate.engine,
            "replicas": 1,
            "verifyManifest": candidate.verify,
            # Its own served name until canary. Production traffic cannot reach it by
            # accident, and the evaluation job addresses it explicitly.
            "servedName": f"{name}-candidate" if track.phase == EVALUATING else name,
        }
        values["canaryWeight"] = track.canary_weight if track.phase == CANARY else 0
    return values


# ----------------------------------------------------------------------------- render


DEPLOY_DIR = REPO_ROOT / "deploy"
GENERATED_HEADER = (
    "# SINH RA TU deploy/state.yaml -- dung sua tay.\n"
    "# Sinh lai: python3 bench/scripts/rollout_state.py render\n"
    "# PR check sinh lai va so sanh, nen mot ban sua tay se bi bat o day chu khong phai\n"
    "# o cum luc 3 gio sang.\n"
)


def _tier_placement(tier: str) -> dict:
    """Where this track's pods are allowed to land.

    The light tier is tainted, so nothing reaches it without asking. A cheap card is
    exactly the kind of resource an unrelated pod drifts onto, and the symptom -- the 1.5B
    sitting Pending while a T4 runs something else -- reads as a capacity problem.

    The main tier keeps the chart's own defaults: it is where everything already runs, and
    restating them here would create a second copy free to drift.
    """
    if tier == "light":
        return {
            "nodeSelector": {"workload": "inference", "tier": "light"},
            "tolerations": [
                {"key": "nvidia.com/gpu", "operator": "Equal", "value": "true",
                 "effect": "NoSchedule"},
                {"key": "tier", "operator": "Equal", "value": "light",
                 "effect": "NoSchedule"},
            ],
        }
    return {}


def _release_values(track: Track, ref: Ref, *, served_name: str, replicas: int) -> dict:
    """Values overriding charts/vllm for one Helm release.

    Expressed as overrides rather than as a new chart on purpose. The existing chart
    already serves one model on one card per pod, with the init container, the probes and
    the resource shapes that produced every measurement this project reports; a rollout
    mechanism is not a reason to re-derive any of that.

    quantization: none is not a claim that the weights are unquantized. The chart uses
    that field only to choose BETWEEN two declared prefixes, and here the state file
    already names the exact one -- vLLM reads quantization_config out of the checkpoint's
    own config.json either way.
    """
    model: dict[str, object] = {"servedName": served_name, "s3Prefix": ref.weights}
    if ref.weights_gib is not None:
        model["weightsGiB"] = ref.weights_gib
    return {
        "mode": "solo-a",
        "quantization": "none",
        # Account-specific values stay out of git: this repo is public and Argo CD syncs
        # from it. `make cluster-config` writes the bucket into this ConfigMap and the
        # role onto the ServiceAccount, both from terraform output.
        "clusterConfigMap": "cluster-config",
        "replicaCount": replicas,
        "verifyManifest": ref.verify,
        "image": _image(ref.engine),
        "models": {"a": model},
        **_tier_placement(track.tier),
    }


def _image(engine: str) -> dict[str, str]:
    """Split an image reference into the fields the chart composes back.

    Digest and tag are kept apart because they are joined differently -- "@" and ":" --
    and a digest joined with a colon is a legal reference to a tag that does not exist,
    so the mistake surfaces as ImagePullBackOff and reads like a registry fault.
    """
    if "@" in engine:
        repository, digest = engine.split("@", 1)
        return {"repository": repository, "digest": digest, "tag": ""}
    repository, _, tag = engine.rpartition(":")
    return {"repository": repository, "tag": tag}


def render(state: State, out_dir: Path = DEPLOY_DIR) -> dict[Path, str]:
    """Return {path: content} for every generated file. Writing is the caller's job.

    Returned rather than written so the PR check can compare without touching the tree:
    a check that regenerates in place and then asks git whether anything moved cannot
    tell "someone edited a generated file" from "the check itself just wrote it".
    """
    files: dict[Path, str] = {}
    for name, track in sorted(state.tracks.items()):
        slug = k8s_name(name)
        files[out_dir / "argocd" / "apps" / f"vllm-{slug}-stable.yaml"] = _application(
            f"vllm-{slug}-stable",
            _release_values(track, track.stable, served_name=name,
                            replicas=TIER_CARDS[track.tier] - (1 if track.candidate else 0)))
        if track.candidate:
            served = f"{name}-candidate" if track.phase == EVALUATING else name
            files[out_dir / "argocd" / "apps" / f"vllm-{slug}-candidate.yaml"] = _application(
                f"vllm-{slug}-candidate",
                _release_values(track, track.candidate, served_name=served, replicas=1))
    return files


def upstream_url(track: str, role: str = "stable", namespace: str = "inference") -> str:
    """Where the guardrail should send this track's requests.

    Derived from the same slug the Application is named after, so the route and the
    Service cannot disagree. Each release owns its own Service now; during canary the
    guardrail holds both URLs and splits between them by weight, which is why the split
    lives there rather than in pod counts.
    """
    # The trailing "-a" is the chart's model key, not decoration: every CD release renders
    # mode: solo-a, so the Service is <release>-a. Leaving it off produces a name that
    # resolves to nothing, and the guardrail reports the upstream as unreachable rather
    # than the route as wrong. tests/test_rollout_state.py renders the chart and asserts
    # this string matches, so the two cannot drift.
    return (f"http://vllm-{k8s_name(track)}-{role}-a.{namespace}"
            ".svc.cluster.local:8000/v1")


def _application(name: str, values: dict) -> str:
    """One Argo CD Application, values and all.

    valuesObject rather than valueFiles. A values file next to the chart would have to be
    referenced as ../../deploy/values/x.yaml, and whether Argo resolves a path that climbs
    out of the chart directory depends on its version and on repo-root checks that cannot
    be exercised without a cluster. Inlining removes the question: there is no path to
    resolve, the Application is self-contained, and the pull request diff shows the exact
    values the cluster will get instead of a filename.

    The candidate's Application simply stops being generated when the candidate is
    abandoned, and the root app-of-apps prunes it. That is why the candidate is its own
    release rather than a second deployment inside one: removing a file is a reviewable
    diff, and pruning is something Argo already does correctly.
    """
    return GENERATED_HEADER + yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": "argocd",
            # Without this Argo deletes the Application record before the resources it
            # owns, and the workloads are left running with nothing managing them.
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "https://github.com/Potato1476/vllm-bench.git",
                "targetRevision": "main",
                "path": "charts/vllm",
                "helm": {"valuesObject": values},
            },
            "destination": {"server": "https://kubernetes.default.svc",
                            "namespace": "inference"},
            "syncPolicy": {
                # Self-heal off on purpose. It would fight `kubectl scale` and every other
                # hand operation during an incident, which is exactly when someone needs
                # the cluster to stay where they put it.
                "automated": {"prune": True, "selfHeal": False},
                "syncOptions": ["CreateNamespace=true"],
                # A pod that syncs weights from S3 and then hashes them takes minutes
                # before it is ready; the default backoff gives up long before that.
                "retry": {"limit": 5, "backoff": {"duration": "30s", "maxDuration": "5m"}},
            },
        },
    }, sort_keys=False, allow_unicode=True)


# ------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("validate", "show", "helm-values", "advance",
                                        "rollback", "render", "check-rendered",
                                        "pause", "resume"))
    ap.add_argument("--file", type=Path, default=STATE_PATH)
    ap.add_argument("--track")
    ap.add_argument("--to")
    ap.add_argument("--weight", type=int, default=0)
    ap.add_argument("--now", default="")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)

    try:
        state = load(a.file)
        if a.command == "validate":
            print(f"  {a.file}: hop le, {len(state.tracks)} track"
                  + (", DANG TAM DUNG" if state.paused else ""))
            for name, track in sorted(state.tracks.items()):
                extra = f" -> {track.candidate.weights}" if track.candidate else ""
                print(f"    {name}: {track.phase}  {track.stable.weights}{extra}")
                if not track.stable.verify:
                    print(f"      CANH BAO: khong kiem manifest -- {track.stable.reason}")
                # A tag can be pushed over; a digest cannot. The engine decides what the
                # answers look like, so an engine that silently changed under a pinned
                # weights version is a model change nobody recorded.
                if "@sha256:" not in track.stable.engine:
                    print(f"      LUU Y: engine ghim theo tag ({track.stable.engine}), "
                          "khong theo digest -- tag co the bi day de")
            return 0
        if a.command in ("render", "check-rendered"):
            wanted = render(state)
            # Files that exist but are no longer generated: an abandoned candidate's
            # Application and values. Leaving one behind would keep a GPU occupied by a
            # version the rollout already rejected.
            existing = {p for p in (DEPLOY_DIR / "argocd" / "apps").glob("*.yaml")}
            stale = sorted(existing - set(wanted))
            if a.command == "render":
                for path, content in sorted(wanted.items()):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                for path in stale:
                    path.unlink()
                print(f"  da sinh {len(wanted)} file"
                      + (f", xoa {len(stale)} file thua" if stale else ""))
                return 0
            drift = [p for p, c in sorted(wanted.items())
                     if not p.is_file() or p.read_text(encoding="utf-8") != c]
            if drift or stale:
                for path in drift:
                    print(f"  LECH: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
                for path in stale:
                    print(f"  THUA: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
                print("\n  Chay: python3 bench/scripts/rollout_state.py render",
                      file=sys.stderr)
                return 1
            print(f"  {len(wanted)} file sinh ra khop voi state.yaml")
            return 0
        if a.command in ("pause", "resume"):
            state.paused = a.command == "pause"
            # A pause with no reason is one nobody can judge later: the next person sees
            # rollout stopped and has to choose between resuming blind and leaving it.
            state.paused_reason = a.reason if state.paused else ""
            if state.paused and not state.paused_reason:
                raise StateError("can --reason: ly do tam dung phai doc duoc trong git")
            dump(state, a.file)
            print(f"  rollout: {'TAM DUNG -- ' + state.paused_reason if state.paused else 'chay lai'}")
            print("  Commit deploy/state.yaml de CI thay.")
            return 0
        if a.command == "show":
            print(json.dumps(state.to_dict(), ensure_ascii=False, indent=1))
            return 0
        if a.command == "helm-values":
            if not a.track:
                raise StateError("can --track")
            print(yaml.safe_dump(helm_values(state, a.track), sort_keys=False,
                                 allow_unicode=True))
            return 0
        if a.command == "rollback":
            if not a.track:
                raise StateError("can --track")
            dump(roll_back(state, a.track), a.file)
            print(f"  {a.track}: da quay ve previous")
            return 0
        if not a.track or not a.to:
            raise StateError("can --track va --to")
        dump(advance(state, a.track, a.to, weight=a.weight, now=a.now), a.file)
        print(f"  {a.track}: -> {a.to}")
        return 0
    except StateError as exc:
        print(f"  LOI: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
