#!/usr/bin/env python3
"""Turn probe records into an availability claim that survives being questioned.

WHY THIS EXISTS AS CODE RATHER THAN A SPREADSHEET CELL

The acceptance criterion is "uptime >= 99.5%, two-week pilot". Running the full stack for
two weeks costs about 338 USD against a 200 USD budget, so the pilot as literally written
cannot happen. That turns out not to matter, because the criterion bundles three claims
that cost wildly different amounts to establish:

  1. What fraction of requests succeed at a given load.
     The load test is the evidence. 600 consecutive successes bound the failure rate below
     0.5% at 95% confidence -- twelve seconds at 50 req/s, or one two-hour session at
     1 req/s, which bounds it below 0.042%. Weeks buy nothing here.

  2. How long recovery takes when something does break.
     Injecting the failure measures this in minutes and measures it better, because it is
     repeatable. Waiting for it is not an experiment.

  3. Failures that only appear with calendar time: leaks, credential expiry, node
     rotation, a dependency that changes under you.
     This one genuinely needs duration -- but it needs CALENDAR COVERAGE, not continuous
     uptime, and fourteen daily sessions cover fourteen days. They also cover fourteen
     deployments, which one continuous run does not, and deployment is where outages
     actually come from.

This module does the arithmetic for 1 and 3, and the episode detection that feeds 2.

WHAT IT REFUSES TO DO

Report a number without an interval. A run of 200 successful probes is consistent with
true availability of 98.5%; saying "100%" from it is not a strong result but an unstated
assumption. Every figure here carries an exact Clopper-Pearson interval, and the verdict
is against the LOWER bound, never the point estimate.

Quietly drop out-of-window probes. The service window is real -- the cluster is destroyed
nightly and MOC analysts work business hours -- but a window is also the easiest way to
make an outage disappear. Excluded probes are counted and printed, including how many of
them failed, so the exclusion can be argued with.

Count time nobody was watching. A three-hour gap between two successful probes is not
three hours of uptime. Intervals longer than --max-gap are removed from the denominator
and reported separately, because unobserved is a third state and pretending otherwise
inflates the result in exactly the direction that flatters it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CONFIDENCE = 0.95


# --- exact binomial intervals -------------------------------------------------------
# Clopper-Pearson, via the regularised incomplete beta function. Wilson or normal
# approximations are cheaper and wrong at the end of the range this project lives in:
# with zero failures the normal interval has zero width, which would let a 20-probe run
# "prove" 100% availability. The exact interval is the one that stays honest at k=0.


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny, eps, max_iter = 1e-300, 3e-16, 300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        num = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + num * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + num / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        num = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + num * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + num / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b) = P(Beta(a,b) <= x)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    # The fraction converges quickly only on one side of the mean; swap and complement
    # on the other. Skipping this costs hundreds of iterations and then accuracy.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson(k: int, n: int, confidence: float = CONFIDENCE) -> tuple[float, float]:
    """Exact two-sided interval for a success proportion k/n."""
    if n <= 0:
        return (0.0, 1.0)
    alpha = 1.0 - confidence
    lower = 0.0 if k == 0 else _beta_quantile(alpha / 2.0, k, n - k + 1)
    upper = 1.0 if k == n else _beta_quantile(1.0 - alpha / 2.0, k + 1, n - k)
    return (lower, upper)


def lower_confidence_bound(k: int, n: int, confidence: float = CONFIDENCE) -> float:
    """One-sided lower bound on a success proportion.

    The verdict uses this rather than the bottom of the two-sided interval, because the
    claim being made is one-sided: "availability is at least 99.5%". Reading the lower end
    of a two-sided 95% interval would really be a 97.5% one-sided claim -- more
    conservative than advertised, and it moves the sample needed from 598 to 736. Being
    stricter than stated is a smaller sin than the reverse, but stating one thing and
    computing another is how a number stops meaning anything.

    At k = n this reduces to alpha^(1/n), the closed form behind the familiar rule of
    three, which is what makes the 598 in the module docstring checkable by hand.
    """
    if n <= 0:
        return 0.0
    if k >= n:
        return (1.0 - confidence) ** (1.0 / n)
    if k <= 0:
        return 0.0
    return _beta_quantile(1.0 - confidence, k, n - k + 1)


def samples_needed(target: float, failures: int = 0,
                   confidence: float = CONFIDENCE) -> int:
    """Smallest n whose lower bound clears `target`, given `failures` observed.

    This is what makes "not enough data yet" a number rather than an opinion, and it is
    why observing one failure is expensive: 598 probes support the 99.5% claim clean, and
    947 are needed after a single failure -- 58% more data to say the same sentence.
    """
    # Two phases, because "the smallest n" has to be exactly the smallest: a geometric
    # walk alone lands on whichever multiple of the step happens to clear the bar, which
    # reported 626 where the answer is 598. Grow to bracket, then bisect. The bound rises
    # monotonically in n for a fixed failure count, so bisection is sound.
    lo = max(failures + 1, 1)
    hi = lo
    while hi < 10_000_000:
        if lower_confidence_bound(hi - failures, hi, confidence) >= target:
            break
        hi = max(hi + 1, int(hi * 1.5))
    else:
        return -1
    while lo < hi:
        mid = (lo + hi) // 2
        if lower_confidence_bound(mid - failures, mid, confidence) >= target:
            hi = mid
        else:
            lo = mid + 1
    return lo


# --- records ------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    ts: float
    ok: bool
    target: str = ""
    latency_ms: float = 0.0
    detail: str = ""
    load_rps: float = 0.0
    session: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts, TZ)


@dataclass(frozen=True)
class ServiceWindow:
    """When the platform is expected to answer.

    Business hours rather than 24/7, and that is a claim about the product, not an excuse
    for the lab: MOC analysts work business hours, and internal SLAs are normally written
    against a service window. Stated here so the report can print it next to the number.
    """

    start_hour: int = 8
    end_hour: int = 18
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)   # Monday..Friday

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(TZ)
        if local.weekday() not in self.weekdays:
            return False
        # Compared as a fractional hour rather than as datetime.time, so that end_hour=24
        # means midnight rather than raising: `--all-hours` passes exactly that, and
        # datetime.time(24) is not a valid time.
        hour = local.hour + local.minute / 60.0 + local.second / 3600.0
        return self.start_hour <= hour < self.end_hour

    def describe(self) -> str:
        if self.start_hour <= 0 and self.end_hour >= 24 and len(self.weekdays) == 7:
            return "24/7 (khong gioi han)"
        days = "T2-T6" if self.weekdays == (0, 1, 2, 3, 4) else str(self.weekdays)
        return f"{self.start_hour:02d}:00-{self.end_hour:02d}:00 {days} (gio Viet Nam)"


@dataclass
class Episode:
    """An uninterrupted run of failing probes: one outage, however many probes saw it."""

    start_ts: float
    end_ts: float
    probes: int
    detail: str

    @property
    def seconds(self) -> float:
        return self.end_ts - self.start_ts


def load_probes(paths: list[Path]) -> list[Probe]:
    probes: list[Probe] = []
    for path in paths:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                probes.append(Probe(
                    ts=float(row["ts"]), ok=bool(row["ok"]),
                    target=str(row.get("target", "")),
                    latency_ms=float(row.get("latency_ms", 0.0)),
                    detail=str(row.get("detail", "")),
                    load_rps=float(row.get("load_rps", 0.0)),
                    session=str(row.get("session", path.stem)),
                ))
            except (ValueError, KeyError, TypeError) as exc:
                print(f"  bo qua {path.name}:{line_no} ({exc})", file=sys.stderr)
    probes.sort(key=lambda p: p.ts)
    return probes


def find_episodes(probes: list[Probe]) -> list[Episode]:
    """Group consecutive failures. One outage is one episode, not N probes.

    This distinction is the whole reason time-availability and request-availability are
    reported separately. Sixty failed probes during one two-minute restart are sixty bad
    requests and one outage, and treating them as sixty independent events would put an
    absurdly tight interval around a sample of size one.
    """
    episodes: list[Episode] = []
    run: list[Probe] = []
    for probe in probes:
        if probe.ok:
            if run:
                episodes.append(Episode(run[0].ts, run[-1].ts, len(run), run[0].detail))
                run = []
        else:
            run.append(probe)
    if run:
        episodes.append(Episode(run[0].ts, run[-1].ts, len(run), run[0].detail))
    return episodes


@dataclass
class Report:
    window: ServiceWindow
    target: float
    in_window: list[Probe] = field(default_factory=list)
    excluded: list[Probe] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    observed_seconds: float = 0.0
    unobserved_seconds: float = 0.0
    downtime_seconds: float = 0.0
    gaps: int = 0

    @property
    def n(self) -> int:
        return len(self.in_window)

    @property
    def failures(self) -> int:
        return sum(1 for p in self.in_window if not p.ok)

    @property
    def request_availability(self) -> float:
        return (self.n - self.failures) / self.n if self.n else 0.0

    @property
    def request_interval(self) -> tuple[float, float]:
        """Two-sided 95% interval, for describing where the true rate probably sits."""
        return clopper_pearson(self.n - self.failures, self.n)

    @property
    def request_lower_bound(self) -> float:
        """One-sided 95% lower bound. This is what the verdict is read from."""
        return lower_confidence_bound(self.n - self.failures, self.n)

    @property
    def time_availability(self) -> float:
        if self.observed_seconds <= 0:
            return 0.0
        return 1.0 - self.downtime_seconds / self.observed_seconds

    @property
    def mttr_seconds(self) -> float:
        return (sum(e.seconds for e in self.episodes) / len(self.episodes)
                if self.episodes else 0.0)

    @property
    def verdict(self) -> str:
        if self.n == 0:
            return "KHONG CO DU LIEU"
        if self.request_lower_bound >= self.target:
            return "DAT"
        if self.request_availability >= self.target:
            return "CHUA DU MAU"
        return "KHONG DAT"


def analyse(probes: list[Probe], window: ServiceWindow, target: float,
            max_gap: float) -> Report:
    report = Report(window=window, target=target)
    for probe in probes:
        (report.in_window if window.contains(probe.when) else report.excluded).append(probe)

    report.episodes = find_episodes(report.in_window)
    report.downtime_seconds = sum(e.seconds for e in report.episodes)

    # Walk consecutive probes and decide what each interval between them witnesses.
    # An interval longer than max_gap is time nobody was looking at, and it belongs in
    # neither the numerator nor the denominator.
    for earlier, later in zip(report.in_window, report.in_window[1:]):
        span = later.ts - earlier.ts
        if span > max_gap:
            report.unobserved_seconds += span
            report.gaps += 1
        else:
            report.observed_seconds += span
    return report


# --- output -------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.4f}%"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} phut"
    return f"{seconds / 3600:.1f} gio"


def render(report: Report) -> str:
    out: list[str] = []
    add = out.append
    lower, upper = report.request_interval
    bound = report.request_lower_bound

    add("=" * 74)
    add("  BAO CAO AVAILABILITY")
    add("=" * 74)
    add(f"  Cua so dich vu : {report.window.describe()}")
    add(f"  Muc tieu       : {_pct(report.target)}")
    add("")

    if report.n == 0:
        add("  Khong co probe nao roi vao cua so dich vu.")
        add("  Kiem tra lai gio chay probe, hoac --start-hour/--end-hour.")
        return "\n".join(out)

    sessions = sorted({p.session for p in report.in_window if p.session})
    first = datetime.fromtimestamp(report.in_window[0].ts, TZ)
    last = datetime.fromtimestamp(report.in_window[-1].ts, TZ)
    add(f"  Mau            : {report.n} probe, {report.failures} that bai")
    add(f"  Trai dai       : {first:%d/%m %H:%M} -> {last:%d/%m %H:%M}"
        f"  ({len(sessions)} phien)")
    add("")

    add("  --- Availability theo REQUEST (bao nhieu phan tram request thanh cong) ---")
    add(f"    Diem           {_pct(report.request_availability)}")
    add(f"    Khoang tin 95% [{_pct(lower)}, {_pct(upper)}]  (hai phia, de mo ta)")
    add(f"    Can duoi 95%   {_pct(bound)}  (MOT phia -- phan quyet doc theo day)")
    add(f"    => {_pct(bound)} {'>=' if bound >= report.target else '<'} "
        f"{_pct(report.target)}")
    add("")

    add("  --- Availability theo THOI GIAN (bao nhieu phan tram thoi gian he thong song) ---")
    add(f"    Quan sat duoc  {_duration(report.observed_seconds)}")
    if report.gaps:
        add(f"    KHONG quan sat {_duration(report.unobserved_seconds)} "
            f"({report.gaps} khoang trong, da loai khoi mau so)")
    add(f"    Downtime       {_duration(report.downtime_seconds)} "
        f"trong {len(report.episodes)} dot")
    add(f"    Ty le          {_pct(report.time_availability)}")
    if report.episodes:
        add(f"    MTTR           {_duration(report.mttr_seconds)} trung binh")
        add("    Cac dot:")
        for ep in report.episodes[:8]:
            when = datetime.fromtimestamp(ep.start_ts, TZ)
            add(f"      {when:%d/%m %H:%M:%S}  {_duration(ep.seconds):>10}  "
                f"{ep.probes} probe  {ep.detail[:38]}")
        if len(report.episodes) > 8:
            add(f"      ... va {len(report.episodes) - 8} dot nua")
    add("")

    # Availability is a curve against offered load, not a scalar. Reporting one number
    # for a platform that was only ever probed at 1 req/s would answer a question nobody
    # asked -- the criterion is about behaviour AT load.
    by_load: dict[float, list[Probe]] = {}
    for probe in report.in_window:
        by_load.setdefault(probe.load_rps, []).append(probe)
    if len(by_load) > 1:
        add("  --- Theo muc tai ---")
        add(f"    {'req/s':>8}  {'probe':>7}  {'loi':>5}  {'diem':>9}  {'can duoi 95%':>13}")
        for rps in sorted(by_load):
            rows = by_load[rps]
            bad = sum(1 for p in rows if not p.ok)
            lo, _ = clopper_pearson(len(rows) - bad, len(rows))
            add(f"    {rps:>8.1f}  {len(rows):>7}  {bad:>5}  "
                f"{_pct((len(rows) - bad) / len(rows)):>9}  {_pct(lo):>13}")
        add("")

    if report.excluded:
        bad = sum(1 for p in report.excluded if not p.ok)
        add("  --- Ngoai cua so dich vu (da loai khoi tinh toan) ---")
        add(f"    {len(report.excluded)} probe, trong do {bad} that bai.")
        add("    In ra de con tranh luan duoc: mot cua so dich vu la cach de nhat de lam")
        add("    bien mat mot su co, nen so bi loai phai nhin thay duoc.")
        add("")

    # An aggregate can pass while a particular load level fails, and the aggregate is the
    # number people quote. The criterion is about behaviour AT load, so a level that falls
    # below the target has to be named even when the total clears it -- averaging over
    # load is how a knee in the curve disappears.
    weak = []
    for rps in sorted(by_load):
        rows = by_load[rps]
        bad = sum(1 for p in rows if not p.ok)
        # Named apart from `bound` above on purpose: reusing it here silently rewrote
        # the figure quoted in the closing sentence with whichever load level happened
        # to be last, which is the sort of error that makes a report worse than none.
        at_load = lower_confidence_bound(len(rows) - bad, len(rows))
        if at_load < report.target:
            weak.append((rps, at_load, len(rows), bad))

    add("  --- Ket luan ---")
    add(f"    {report.verdict}")
    if weak and report.verdict == "DAT":
        add("")
        add("    NHUNG: con so gop dat khong co nghia moi muc tai deu dat.")
        for rps, at_load, n, bad in weak:
            need = samples_needed(report.target, bad)
            add(f"      {rps:g} req/s: can duoi {_pct(at_load)} < {_pct(report.target)} "
                f"({n} probe, {bad} loi)")
            add(f"        -> {'can >= ' + str(need) + ' probe o muc nay' if need > n else 'that su khong dat'}")
        add("    Tieu chi noi ve hanh vi O MUC TAI, nen muc yeu nhat moi la cau tra loi.")
    if report.verdict == "CHUA DU MAU":
        need = samples_needed(report.target, report.failures)
        add(f"    Diem so dat muc tieu nhung mau con nho: can >= {need} probe "
            f"voi {report.failures} loi de can duoi vuot {_pct(report.target)}.")
        add(f"    Dang co {report.n}. Con thieu {max(0, need - report.n)}.")
    elif report.verdict == "KHONG DAT":
        add(f"    Can duoi {_pct(bound)} nam duoi muc tieu {_pct(report.target)}.")
        add("    Them mau khong cuu duoc dieu nay -- phai giam so su co hoac MTTR.")
    else:
        add(f"    Voi {report.n} probe va {report.failures} loi, can duoi cua khoang tin")
        add(f"    95% mot phia la {_pct(bound)}, tren muc tieu {_pct(report.target)}.")
    add("")
    add("  Luu y pham vi: ket luan nay noi ve cua so dich vu o tren, tren tap phien da")
    add("  quan sat. No KHONG noi gi ve hong hoc chi xuat hien sau nhieu tuan chay lien")
    add("  tuc (ro bo nho, het han chung chi, xoay node) -- nhung thu do can do rieng.")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path,
                        help="tep JSONL ghi ket qua probe (nhieu phien deu duoc)")
    parser.add_argument("--target", type=float, default=0.995,
                        help="muc availability phai dat (mac dinh 0.995)")
    parser.add_argument("--start-hour", type=int, default=8)
    parser.add_argument("--end-hour", type=int, default=18)
    parser.add_argument("--all-hours", action="store_true",
                        help="bo cua so dich vu, tinh 24/7")
    parser.add_argument("--max-gap", type=float, default=300.0,
                        help="khoang cach giua hai probe lon hon nguong nay (giay) "
                             "duoc coi la khong quan sat, khong phai uptime")
    parser.add_argument("--json", action="store_true", help="xuat JSON thay vi bao cao")
    args = parser.parse_args()

    files = [p for p in args.paths if p.is_file()]
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
    if not files:
        print("khong tim thay tep probe nao", file=sys.stderr)
        return 1

    window = (ServiceWindow(0, 24, tuple(range(7))) if args.all_hours
              else ServiceWindow(args.start_hour, args.end_hour))
    report = analyse(load_probes(files), window, args.target, args.max_gap)

    if args.json:
        lower, upper = report.request_interval
        print(json.dumps({
            "verdict": report.verdict,
            "target": args.target,
            "window": window.describe(),
            "probes": report.n,
            "failures": report.failures,
            "request_availability": report.request_availability,
            "request_ci95_two_sided": [lower, upper],
            "request_lower_bound_95": report.request_lower_bound,
            "time_availability": report.time_availability,
            "observed_seconds": report.observed_seconds,
            "unobserved_seconds": report.unobserved_seconds,
            "downtime_seconds": report.downtime_seconds,
            "episodes": len(report.episodes),
            "mttr_seconds": report.mttr_seconds,
            "excluded_probes": len(report.excluded),
        }, ensure_ascii=False, indent=2))
    else:
        print(render(report))

    # Exit non-zero only on a claim that fails outright. "Not enough data yet" is a
    # normal state on day three of a two-week pilot, not a build failure.
    return 1 if report.verdict == "KHONG DAT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
