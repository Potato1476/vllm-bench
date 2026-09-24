"""The arithmetic behind the availability claim, checked against values it cannot fudge.

A statistics helper is the easiest kind of code to get quietly wrong: it returns a
plausible number for every input, and nothing downstream can tell a correct interval from
a confident one. So the interval is checked against closed forms that can be derived by
hand, and the report logic is checked against the three ways it is meant to refuse to
overclaim -- too few samples, unobserved gaps, and probes outside the service window.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from bench.scripts.availability import (
    Probe,
    render,
    ServiceWindow,
    TZ,
    analyse,
    clopper_pearson,
    find_episodes,
    lower_confidence_bound,
    samples_needed,
)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    """A timestamp in September 2026. 2026-09-21 is a Monday.

    Minutes and seconds are added as an offset rather than passed to the constructor, so
    callers can write `at(21, 9, 0, i * 10)` without hand-carrying into the next minute.
    """
    base = datetime(2026, 9, day, hour, tzinfo=TZ)
    return (base + timedelta(minutes=minute, seconds=second)).timestamp()


class IntervalTest(unittest.TestCase):
    def test_a_perfect_run_matches_the_closed_form(self) -> None:
        # With no failures the one-sided lower bound collapses to alpha^(1/n), checkable
        # without any special function -- so this pins the incomplete beta against
        # arithmetic rather than against itself.
        for n in (10, 100, 598, 5000):
            self.assertAlmostEqual(lower_confidence_bound(n, n), 0.05 ** (1 / n),
                                   places=9, msg=f"n={n}")

    def test_the_two_sided_upper_bound_also_has_a_closed_form(self) -> None:
        # Two-sided puts alpha/2 in each tail, so the k=0 upper bound is
        # 1 - (alpha/2)^(1/n) and NOT the one-sided 1 - alpha^(1/n). Mixing the two is
        # the easiest way to quote a sample size that is wrong by 25%.
        for n in (10, 100, 598):
            _, upper = clopper_pearson(0, n)
            self.assertAlmostEqual(upper, 1 - 0.025 ** (1 / n), places=9, msg=f"n={n}")

    def test_the_598_that_the_whole_plan_rests_on(self) -> None:
        # The claim in the module docstring: ~600 consecutive successes put availability
        # above 99.5%. If this moves, every proposed experiment size moves with it.
        self.assertGreaterEqual(lower_confidence_bound(598, 598), 0.995)
        self.assertLess(lower_confidence_bound(597, 597), 0.995)

    def test_a_known_two_sided_interval(self) -> None:
        # 5 failures in 100: the textbook Clopper-Pearson interval for 0.05 is
        # [0.0164, 0.1128]. Checks the incomplete beta on both tails at once.
        lower, upper = clopper_pearson(95, 100)
        self.assertAlmostEqual(1 - upper, 0.01643, places=5)
        self.assertAlmostEqual(1 - lower, 0.11283, places=5)

    def test_an_interval_is_never_zero_width(self) -> None:
        # The reason for not using a normal approximation: at k=n it reports +-0, which
        # would let twenty probes "prove" perfect availability.
        lower, upper = clopper_pearson(20, 20)
        self.assertEqual(upper, 1.0)
        self.assertLess(lower, 0.87)

    def test_one_failure_costs_most_of_a_sample(self) -> None:
        clean = samples_needed(0.995, failures=0)
        after_one = samples_needed(0.995, failures=1)
        self.assertEqual(clean, 598)
        # One failure costs ~58% more data to recover the same claim -- the Poisson
        # ratio 4.74/2.996. This is why the report prints the number you now need
        # instead of vaguely saying "collect more".
        self.assertAlmostEqual(after_one / clean, 4.744 / 2.996, places=2)


class EpisodeTest(unittest.TestCase):
    def test_one_outage_is_one_episode_however_many_probes_saw_it(self) -> None:
        probes = [Probe(ts=at(21, 9, 0, s), ok=(s not in (10, 20, 30)))
                  for s in range(0, 60, 10)]
        episodes = find_episodes(probes)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].probes, 3)
        self.assertEqual(episodes[0].seconds, 20)

    def test_separate_outages_stay_separate(self) -> None:
        flags = [True, False, True, True, False, False, True]
        probes = [Probe(ts=at(21, 9, 0, i * 10), ok=ok) for i, ok in enumerate(flags)]
        self.assertEqual(len(find_episodes(probes)), 2)


class WindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.window = ServiceWindow()

    def test_business_hours_only(self) -> None:
        self.assertTrue(self.window.contains(datetime.fromtimestamp(at(21, 9), TZ)))
        self.assertFalse(self.window.contains(datetime.fromtimestamp(at(21, 7), TZ)))
        self.assertFalse(self.window.contains(datetime.fromtimestamp(at(21, 18), TZ)))
        # 2026-09-26 is a Saturday.
        self.assertFalse(self.window.contains(datetime.fromtimestamp(at(26, 10), TZ)))

    def test_nightly_teardown_does_not_read_as_downtime(self) -> None:
        """The reason the window exists at all.

        The cluster is destroyed every evening. Counted against 24/7 the platform would
        score about 15% availability while behaving perfectly, which is a number that
        tells you only that the wrong thing was measured.
        """
        # 700 in-window probes, because 120 would be refused for a different and equally
        # correct reason -- too small a sample -- and the point here is the window.
        probes = [Probe(ts=at(21, 9, 0, i * 30), ok=True) for i in range(700)]
        probes += [Probe(ts=at(21, 21, 0, i * 60), ok=False, detail="cluster down")
                   for i in range(120)]

        in_window = analyse(probes, ServiceWindow(), 0.995, max_gap=300)
        self.assertEqual(in_window.failures, 0)
        self.assertEqual(len(in_window.excluded), 120)
        self.assertEqual(in_window.verdict, "DAT")

        around_the_clock = analyse(probes, ServiceWindow(0, 24, tuple(range(7))),
                                   0.995, max_gap=300)
        self.assertEqual(around_the_clock.verdict, "KHONG DAT")


class ReportTest(unittest.TestCase):
    def test_a_short_clean_run_refuses_to_claim_the_target(self) -> None:
        # 200 perfect probes is consistent with true availability of 98.5%. Reporting
        # "100%" from it would be an assumption wearing a number's clothes.
        probes = [Probe(ts=at(21, 9, 0, i * 60), ok=True) for i in range(200)]
        report = analyse(probes, ServiceWindow(), 0.995, max_gap=300)
        self.assertEqual(report.request_availability, 1.0)
        self.assertEqual(report.verdict, "CHUA DU MAU")
        self.assertGreater(samples_needed(0.995, 0), report.n)

    def test_a_long_clean_run_does_claim_it(self) -> None:
        probes = [Probe(ts=at(21, 8, 0, i * 30), ok=True) for i in range(1200)]
        report = analyse(probes, ServiceWindow(), 0.995, max_gap=300)
        self.assertEqual(report.verdict, "DAT")
        self.assertGreaterEqual(report.request_lower_bound, 0.995)

    def test_time_not_watched_is_not_time_up(self) -> None:
        """A gap is a third state, and it must not land in the numerator.

        Two successful probes three hours apart witness two instants, not three hours.
        Counting the gap as uptime inflates the result in precisely the direction that
        flatters it, which is how a laptop being shut for lunch becomes evidence.
        """
        probes = [Probe(ts=at(21, 8, 0, i * 60), ok=True) for i in range(30)]
        probes += [Probe(ts=at(21, 13, 0, i * 60), ok=True) for i in range(30)]
        report = analyse(probes, ServiceWindow(), 0.995, max_gap=300)
        self.assertEqual(report.gaps, 1)
        self.assertGreater(report.unobserved_seconds, 4 * 3600)
        # Denominator is the two observed half-hours, not the four-hour span.
        self.assertLess(report.observed_seconds, 2 * 1800 + 120)

    def test_a_real_outage_shows_in_both_numbers_and_in_mttr(self) -> None:
        probes = [Probe(ts=at(21, 9, 0, i * 10), ok=True) for i in range(120)]
        probes += [Probe(ts=at(21, 9, 20, i * 10), ok=False, detail="502 upstream")
                   for i in range(12)]
        probes += [Probe(ts=at(21, 9, 22, i * 10), ok=True) for i in range(120)]
        report = analyse(probes, ServiceWindow(), 0.995, max_gap=300)

        self.assertEqual(len(report.episodes), 1)
        self.assertEqual(report.failures, 12)
        self.assertAlmostEqual(report.mttr_seconds, 110, delta=1)
        # Request- and time-availability answer different questions and only coincide
        # when failures are evenly spread. A burst separates them, which is the point.
        self.assertLess(report.request_availability, 0.96)
        self.assertLess(report.time_availability, 1.0)
        self.assertEqual(report.verdict, "KHONG DAT")


class RenderTest(unittest.TestCase):
    def test_a_failing_load_level_is_named_even_when_the_total_passes(self) -> None:
        """An aggregate that passes can hide a load level that does not.

        The criterion is about behaviour at load, so averaging over load is how a knee in
        the curve disappears into a number somebody then quotes.
        """
        # Enough clean volume at 1 req/s that the total clears the bar, while the much
        # smaller sample at 10 req/s does not. This is the realistic shape: the level
        # under stress is the one you can afford least time at.
        probes = [Probe(ts=at(21, 9, 0, i), ok=True, load_rps=1.0) for i in range(10000)]
        probes += [Probe(ts=at(22, 9, 0, i), ok=True, load_rps=1.0) for i in range(10000)]
        probes += [Probe(ts=at(23, 9, 0, i), ok=(i >= 20), load_rps=10.0,
                         detail="502 upstream") for i in range(700)]
        report = analyse(probes, ServiceWindow(), 0.995, max_gap=300)
        text = render(report)

        self.assertEqual(report.verdict, "DAT")
        self.assertIn("NHUNG", text)
        self.assertIn("10 req/s", text)
        # And the closing sentence must still quote the AGGREGATE bound. A first version
        # reused one variable for both and printed the weakest load level's figure as if
        # it were the overall result.
        aggregate = f"{report.request_lower_bound * 100:.4f}%"
        self.assertIn(aggregate, text.split("Ket luan")[1])


if __name__ == "__main__":
    unittest.main()
