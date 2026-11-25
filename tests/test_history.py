"""历史团组估计：删失感知、强退与不续约分离、组合可校验门槛。"""

from _base import CarePlanTestCase
from careplan.history import (
    cohort_facts, estimate_combos, wilson_interval, poisson_interval,
)
from careplan.sources import Policy


POLICY = Policy("山前", min_cohorts=4, min_outcome_weeks=6,
                min_interval_coverage=0.80, max_unfunded_risk=0.10, max_cost_cents=750000)


class HistoryTests(CarePlanTestCase):
    def test_wilson_and_poisson_intervals(self):
        p, lo, hi = wilson_interval(8, 10)
        self.assertAlmostEqual(p, 0.8)
        self.assertTrue(0.0 <= lo <= p <= hi <= 1.0)
        h, hlo, hhi = poisson_interval(3, 100)
        self.assertAlmostEqual(h, 0.03)
        self.assertTrue(0.0 <= hlo <= h <= hhi)

    def test_relocated_and_nonrenewal_are_separate_rates(self):
        facts = cohort_facts(self.bundle.past, "山前", self.date("2026-01-18"))
        rel = sum(f.closed_relocated for f in facts)
        non = sum(f.closed_nonrenewal for f in facts)
        renew = sum(f.renew_accepted for f in facts)
        # 两类结项都存在且被分别计数，没有合并成同一个比例
        self.assertGreater(rel, 0)
        self.assertGreater(non, 0)
        self.assertGreater(renew, 0)

    def test_renewal_rate_excludes_censored(self):
        estimates = estimate_combos(self.bundle.past, self.bundle.cutoffs, POLICY, {})
        e = next(x for x in estimates if x.station == "山前" and x.combo == (2, 45))
        # 待续约未给结论的家庭不进续约二项试验
        self.assertEqual(e.renew_accepted + e.nonrenewal, 50)
        self.assertGreater(e.censored_renewals, 0)
        self.assertIsNotNone(e.renew_rate)

    def test_combo_without_coverage_is_not_estimable(self):
        estimates = estimate_combos(self.bundle.past, self.bundle.cutoffs, POLICY, {})
        for e in estimates:
            if e.station == "山前":
                self.assertFalse(e.estimable)
                self.assertIn("覆盖率", e.reason)

    def test_combo_becomes_estimable_with_adequate_coverage(self):
        coverage = {("山前", 2, 45): 1.0, ("山前", 1, 45): 1.0,
                    ("山前", 2, 60): 1.0, ("山前", 1, 60): 1.0}
        estimates = estimate_combos(self.bundle.past, self.bundle.cutoffs, POLICY, coverage)
        for e in estimates:
            if e.station == "山前":
                self.assertTrue(e.estimable, e.reason)

    def test_station_without_policy_is_never_estimable(self):
        estimates = estimate_combos(self.bundle.past, self.bundle.cutoffs, None, {})
        self.assertTrue(all(not e.estimable for e in estimates if e.station == "关桥"))

    def test_qiaoqiao_has_too_few_cohorts_under_policy(self):
        # 关桥每组合仅 3 个团组，低于 min_cohorts=4
        policy = Policy("关桥", 4, 6, 0.80, 0.10, 750000)
        estimates = estimate_combos(
            self.bundle.past, self.bundle.cutoffs, policy,
            {("关桥", f, m): 1.0 for f in (1, 2) for m in (45, 60)},
        )
        for e in estimates:
            if e.station == "关桥":
                self.assertFalse(e.estimable)
                self.assertIn("min_cohorts", e.reason)
