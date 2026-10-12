#!/usr/bin/env python3
"""One step of every rollout: read S3, decide, edit deploy/state.yaml. Run by rollout.yml.

    rollout_controller.py step --bucket <artifacts bucket> --summary out.json
    rollout_controller.py step --bucket … --dry-run        # decide, write nothing
    rollout_controller.py check-catalog

WHAT ONE STEP DOES

For each track, at most one move:

    idle        a version newer than the watermark has _READY under the track's watch
                prefix -> run the CPU gates (check_model.py) -> candidate, or rejected
    evaluating  runs/rollout/<id>/eval.json   ok -> canary     failed -> rejected
    canary      runs/rollout/<id>/canary.json ok -> promoting  failed -> rejected
    promoting   PROMOTE_SETTLE after the PR -> watching (pods roll one at a time meanwhile)
    watching    runs/rollout/<id>/watch.json  ok -> idle       failed -> roll back

The output is the edited state file plus the re-rendered tree. Nothing here touches the
cluster or AWS beyond reading: rollout.yml turns the change into a pull request, the PR
check validates it, it merges, and Argo CD applies it. One step per run and one PR per
step is slow on purpose -- every move is a reviewable commit, and the history of
deploy/state.yaml IS the rollout log.

WHAT IT NEVER DOES: TIME A JOB OUT

This runs in GitHub Actions and cannot see the cluster. A missing report means either
"the Job is still working" or "the cluster is switched off for the night", and the
second is the common case. Rejecting on silence would throw away every candidate that was
mid-evaluation at 6pm. The Jobs own their deadlines and always leave a report; here a
long silence is only flagged, as `stalled`, for a person to look at.

The one wait timed here is PROMOTE_SETTLE, because nothing reports when a roll finishes
and the watch window should not open while half the pods are still the old version.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.scripts import check_model  # noqa: E402
from bench.scripts import rollout_state as rs  # noqa: E402

CATALOG_PATH = rs.REPO_ROOT / "deploy" / "catalog.yaml"
PROMOTE_SETTLE = timedelta(minutes=25)
STALLED_AFTER = timedelta(hours=6)


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Watch:
    track: str
    watch: str
    allowed_signers: tuple[str, ...]
    auto_rollout: bool = True
    canary_weight: int = 25


def load_catalog(path: Path = CATALOG_PATH, state: rs.State | None = None) -> dict[str, Watch]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise CatalogError(f"{path}: goc phai la mot anh xa")
    out: dict[str, Watch] = {}
    for track, body in raw.items():
        if not isinstance(body, dict):
            raise CatalogError(f"{track}: phai la mot anh xa")
        watch = body.get("watch")
        # Exactly models/<name>/: the contract's layout, and the only prefix the
        # Actions role can list. A watch on "models/" would see every track's versions.
        if not isinstance(watch, str) or not watch.startswith("models/") \
                or not watch.endswith("/") or watch.count("/") != 2:
            raise CatalogError(f"{track}.watch phai co dang models/<ten>/, nhan {watch!r}")
        signers = body.get("allowed_signers")
        if not isinstance(signers, list) or not signers \
                or not all(isinstance(s, str) and s for s in signers):
            # Empty would mean "any key in deploy/signers/", which is not something
            # anyone should get by leaving a field out.
            raise CatalogError(f"{track}.allowed_signers phai la danh sach khong rong")
        for signer in signers:
            if not (check_model.SIGNERS_DIR / f"{signer}.pub").is_file():
                raise CatalogError(f"{track}: khong co deploy/signers/{signer}.pub")
        weight = body.get("canary_weight", 25)
        if not isinstance(weight, int) or not 1 <= weight <= 50:
            raise CatalogError(f"{track}.canary_weight phai trong 1..50")
        auto = body.get("auto_rollout", True)
        if not isinstance(auto, bool):
            raise CatalogError(f"{track}.auto_rollout phai la true/false")
        if state is not None and track not in state.tracks:
            raise CatalogError(f"{track} co trong catalog nhung khong co trong state.yaml")
        out[track] = Watch(track, watch, tuple(signers), auto, weight)
    watches = [w.watch for w in out.values()]
    if len(watches) != len(set(watches)):
        raise CatalogError("hai track cung theo doi mot prefix")
    return out


# ------------------------------------------------------------------------- observing


@dataclass(frozen=True)
class Version:
    prefix: str             # models/<name>/<version>, no trailing slash, no bucket
    published_at: datetime  # _READY's LastModified


class Store(Protocol):
    def list_ready(self, watch: str) -> list[Version]: ...
    def read_json(self, key: str) -> dict | None: ...


def _aws(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", *cmd], capture_output=True, text=True)


def parse_time(value: str) -> datetime:
    """S3 says 2026-10-12T09:14:00.000Z or +00:00 depending on the call; state says Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class S3Store:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def list_ready(self, watch: str) -> list[Version]:
        out, token = [], None
        while True:
            cmd = ["s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", watch,
                   "--output", "json"]
            if token:
                cmd += ["--starting-token", token]
            proc = _aws(cmd)
            if proc.returncode != 0:
                raise RuntimeError(f"khong liet ke duoc s3://{self.bucket}/{watch}: "
                                   f"{proc.stderr.strip()[:200]}")
            page = json.loads(proc.stdout or "{}")
            for row in page.get("Contents", []):
                rest = row["Key"][len(watch):]
                # <version>/_READY, exactly one level down. A _READY deeper than that is
                # not a version this layout knows how to name.
                parts = rest.split("/")
                if len(parts) == 2 and parts[1] == "_READY" and parts[0]:
                    out.append(Version(f"{watch}{parts[0]}", parse_time(row["LastModified"])))
            token = page.get("NextToken")
            if not token:
                return out

    def read_json(self, key: str) -> dict | None:
        proc = _aws(["s3", "cp", f"s3://{self.bucket}/{key}", "-", "--only-show-errors"])
        if proc.returncode != 0:
            if "404" in proc.stderr or "Not Found" in proc.stderr or "NoSuchKey" in proc.stderr:
                return None
            raise RuntimeError(f"khong doc duoc s3://{self.bucket}/{key}: {proc.stderr[:200]}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            # A half-written report is not a verdict. Treated as absent; the Job writes
            # it whole with one PutObject, so this only happens to a hand-edited file.
            return None
        return data if isinstance(data, dict) else None


GateResult = tuple[bool, list[str], dict]
Gate = Callable[[str, Watch], GateResult]


def s3_gate(bucket: str) -> Gate:
    """The VALID and COMPATIBLE gates, on the bucket, before anything costs a GPU."""
    def run(prefix: str, entry: Watch) -> GateResult:
        name, version = check_model.parse_prefix(prefix)
        try:
            report = check_model.check(
                check_model.S3Source(f"s3://{bucket}/{prefix}/"),
                expect_name=name, expect_version=version,
                allowed_signers=list(entry.allowed_signers))
        except check_model.CheckError as exc:
            return False, [str(exc)], {}
        return report.ok, [str(f) for f in report.findings], dict(report.facts)
    return run


# --------------------------------------------------------------------------- deciding


@dataclass
class Event:
    track: str
    kind: str          # candidate | rejected | advanced | promoted | rolled_back |
                       # watched | stalled | skipped
    message: str
    rollout_id: str = ""
    report: str = ""
    # What rollout.yml does with GitHub issues. Every promote opens one -- nobody approved
    # it beforehand, so everyone must be able to see it afterwards -- and a passed watch
    # closes it. Rejections open one, so a producer learns why their version did not ship.
    issue: str = ""    # "" | open | close

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Step:
    events: list[Event] = field(default_factory=list)
    changed: bool = False

    def add(self, event: Event, *, changes: bool = True) -> None:
        self.events.append(event)
        self.changed = self.changed or changes


def step(state: rs.State, catalog: dict[str, Watch], store: Store, gate: Gate,
         now: datetime) -> Step:
    out = Step()
    if state.paused:
        out.add(Event("*", "skipped", f"rollout dang tam dung: {state.paused_reason}"),
                changes=False)
        return out
    stamp = fmt_time(now)
    for name in sorted(state.tracks):
        track = state.track(name)
        entry = catalog.get(name)
        if track.phase == rs.IDLE:
            if entry is not None:
                _propose(state, name, entry, store, gate, stamp, out)
        elif track.phase in (rs.EVALUATING, rs.CANARY):
            _judge_candidate(state, name, entry, store, now, out)
        elif track.phase == rs.PROMOTING:
            since = parse_time(track.phase_since) if track.phase_since else None
            if since is None or now - since >= PROMOTE_SETTLE:
                rs.advance(state, name, rs.WATCHING, now=stamp)
                out.add(Event(name, "advanced", "cac pod da cuon xong; bat dau theo doi 60 phut",
                              rs.rollout_id(name, track.stable)))
        elif track.phase == rs.WATCHING:
            _judge_watch(state, name, store, now, out)
    return out


def _propose(state: rs.State, name: str, entry: Watch, store: Store, gate: Gate,
             stamp: str, out: Step) -> None:
    track = state.track(name)
    if not entry.auto_rollout:
        return
    blocked = rs.can_host_candidate(track)
    if blocked:
        # Khong phai su co, nen khong do ca luot chay: cac track khac van tien duoc.
        out.add(Event(name, "skipped", f"chua rollout duoc -- {blocked}"), changes=False)
        return
    floor = parse_time(track.watermark) if track.watermark else None
    known = {track.stable.weights} | ({track.previous.weights} if track.previous else set()) \
        | {r["weights"] for r in track.rejected}
    fresh = [v for v in store.list_ready(entry.watch)
             if (floor is None or v.published_at > floor) and v.prefix not in known]
    if not fresh:
        return
    newest = max(fresh, key=lambda v: (v.published_at, v.prefix))
    # The watermark moves past EVERY fresh version now, not only the one taken. Two
    # versions landing in one interval means the older one is already superseded;
    # proposing it after the newer one ships would roll production backwards.
    track.watermark = fmt_time(newest.published_at)
    skipped = sorted(v.prefix for v in fresh if v is not newest)
    if skipped:
        out.add(Event(name, "skipped", f"bo qua {len(skipped)} version cu hon: "
                      + ", ".join(skipped)))

    ok, reasons, facts = gate(newest.prefix, entry)
    served = facts.get("served_name")
    if ok and served and served != name:
        ok, reasons = False, [f"manifest served_name={served!r}, track nay la {name!r}"]
    if not ok:
        rs.reject(state, name, "; ".join(reasons)[:500], now=stamp, weights=newest.prefix)
        out.add(Event(name, "rejected",
                      f"{newest.prefix} truot cong kiem truoc GPU: " + "; ".join(reasons),
                      issue="open"))
        return
    weights_gib = facts.get("weights_gib")
    candidate = rs.Ref(newest.prefix, track.stable.engine, True, "",
                       float(weights_gib) if weights_gib is not None else None)
    if candidate.weights == track.stable.weights:
        return
    rs.set_candidate(state, name, candidate, now=stamp)
    out.add(Event(name, "candidate",
                  f"{newest.prefix} qua cong kiem (ky boi {facts.get('signer', '?')}); "
                  "dua len 1 GPU de danh gia", rs.rollout_id(name, candidate)))


def _stalled(name: str, track: rs.Track, rid: str, kind: str, now: datetime,
             out: Step) -> None:
    if track.phase_since and now - parse_time(track.phase_since) > STALLED_AFTER:
        out.add(Event(name, "stalled",
                      f"o phase {track.phase} tu {track.phase_since}, chua co "
                      f"{rs.report_key(rid, kind)}. Cum co dang bat khong? Job co chay khong?",
                      rid), changes=False)


def _judge_candidate(state: rs.State, name: str, entry: Watch | None, store: Store,
                     now: datetime, out: Step) -> None:
    track = state.track(name)
    assert track.candidate is not None
    rid = rs.rollout_id(name, track.candidate)
    kind = "eval" if track.phase == rs.EVALUATING else "canary"
    key = rs.report_key(rid, kind)
    report = store.read_json(key)
    stamp = fmt_time(now)
    if report is None:
        _stalled(name, track, rid, kind, now, out)
        return
    weights = track.candidate.weights
    if not report.get("ok"):
        reasons = "; ".join(report.get("reasons") or ["khong ro ly do"])
        rs.reject(state, name, f"{kind}: {reasons}"[:500], now=stamp)
        out.add(Event(name, "rejected", f"{weights} truot buoc {kind}: {reasons}",
                      rid, key, issue="open"))
        return
    if kind == "eval":
        weight = entry.canary_weight if entry else 25
        rs.advance(state, name, rs.CANARY, weight=weight, now=stamp)
        out.add(Event(name, "advanced", f"{weights} dat danh gia; canary {weight}%", rid, key))
    else:
        old = track.stable.weights
        rs.advance(state, name, rs.PROMOTING, now=stamp)
        out.add(Event(name, "promoted", f"{weights} dat canary; thay {old}. Cac pod cuon tung "
                      "cai mot, roi theo doi 60 phut.", rid, key, issue="open"))


def _judge_watch(state: rs.State, name: str, store: Store, now: datetime, out: Step) -> None:
    track = state.track(name)
    rid = rs.rollout_id(name, track.stable)
    key = rs.report_key(rid, "watch")
    report = store.read_json(key)
    stamp = fmt_time(now)
    if report is None:
        _stalled(name, track, rid, "watch", now, out)
        return
    weights = track.stable.weights
    if report.get("ok"):
        rs.advance(state, name, rs.IDLE, now=stamp)
        out.add(Event(name, "watched", f"{weights} qua 60 phut theo doi", rid, key,
                      issue="close"))
        return
    reasons = "; ".join(report.get("reasons") or ["khong ro ly do"])
    previous = track.previous.weights if track.previous else "?"
    rs.roll_back(state, name, reason=f"watch: {reasons}"[:500], now=stamp)
    out.add(Event(name, "rolled_back", f"{weights} truot buoc theo doi ({reasons}); quay ve "
                  f"{previous}", rid, key, issue="open"))


# ------------------------------------------------------------------------------- main


def summarize(result: Step) -> dict:
    moves = [e for e in result.events if e.kind not in ("skipped", "stalled")]
    title = ("rollout: " + "; ".join(f"{e.track} {e.kind}" for e in moves))[:120] \
        if moves else ""
    lines = [f"- **{e.track}** `{e.kind}`: {e.message}"
             + (f"  \n  bao cao: `{e.report}`" if e.report else "") for e in result.events]
    return {"changed": result.changed, "title": title, "body": "\n".join(lines),
            "events": [e.to_dict() for e in result.events]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("step", "check-catalog"))
    ap.add_argument("--bucket")
    ap.add_argument("--state", type=Path, default=rs.STATE_PATH)
    ap.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    ap.add_argument("--now", help="ISO time, for replaying a decision; default: now")
    ap.add_argument("--summary", type=Path, help="write the PR title/body and events here")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        state = rs.load(a.state)
        catalog = load_catalog(a.catalog, state)
    except (rs.StateError, CatalogError) as exc:
        print(f"  LOI: {exc}", file=sys.stderr)
        return 1
    if a.command == "check-catalog":
        for entry in catalog.values():
            print(f"  {entry.track}: {entry.watch}  ky boi {', '.join(entry.allowed_signers)}"
                  + ("" if entry.auto_rollout else "  (khong tu dua len)"))
        return 0
    if not a.bucket:
        print("  LOI: can --bucket", file=sys.stderr)
        return 1

    now = parse_time(a.now) if a.now else datetime.now(timezone.utc)
    try:
        result = step(state, catalog, S3Store(a.bucket), s3_gate(a.bucket), now)
    except (rs.StateError, RuntimeError) as exc:
        print(f"  LOI: {exc}", file=sys.stderr)
        return 1
    summary = summarize(result)
    for event in result.events:
        print(f"  {event.track}: {event.kind} -- {event.message}")
    if not result.events:
        print("  khong co gi de lam")
    if result.changed and not a.dry_run:
        rs.dump(state, a.state)
        written, removed = rs.write_rendered(state)
        print(f"  state.yaml da sua; sinh {written} file, xoa {removed}")
    if a.summary:
        a.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
