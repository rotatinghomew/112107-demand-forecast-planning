"""滚动截点回验：只用当时已知凭据，区间覆盖率与口径纪律。"""

import datetime as dt

from _base import CarePlanTestCase
from careplan.backtest import (
    backtest_combo, rolling_cutoffs, summarize_coverage,
)
from careplan.timeutils import add_weeks, week_start


class BacktestTests(CarePlanTestCase):
    def _cuts(self, station, observed, horizon=6):
        first = min(e.effective for e in self.bundle.past if e.station == station)
        last = add_weeks(week_start(observed), -horizon)
        return rolling_cutoffs(add_weeks(week_start(first), 8), last, 4)

    def test_shanqian_coverage_meets_threshold(self):
        observed = self.date("2026-01-18")
        for freq, minutes in [(1, 45), (1, 60), (2, 45), (2, 60)]:
            fs = backtest_combo(self.bundle.past, "山前", freq, minutes,
                                self._cuts("山前", observed), observed, 6)
            summary = summarize_coverage(fs)[0]
            self.assertIsNotNone(summary.coverage)
            self.assertGreaterEqual(summary.coverage, 0.80)

    def test_actuals_exclude_cohorts_started_after_cutoff(self):
        # 真值只核对截点已 start 的在户团组：上界=在户满负荷时真值不得越界
        observed = self.date("2026-01-18")
        cuts = [self.date("2025-11-03")]
        fs = backtest_combo(self.bundle.past, "山前", 2, 45, cuts, observed, 2)
        for f in fs:
            if f.covered is not None:
                self.assertLessEqual(f.actual_minutes, f.high_minutes)

    def test_target_beyond_observation_is_uncheckable(self):
        # 关桥观察止日更早，超出部分必须标 uncheckable 而非失败
        observed = self.date("2025-12-28")
        fs = backtest_combo(self.bundle.past, "关桥", 2, 45,
                            [self.date("2025-12-15")], observed, 6)
        self.assertTrue(any(f.covered is None for f in fs))
        self.assertTrue(all("观察止日" in f.reason for f in fs if f.covered is None))

    def test_upper_bound_allows_full_load_for_return_rights(self):
        observed = self.date("2026-01-18")
        fs = backtest_combo(self.bundle.past, "山前", 2, 45,
                            [self.date("2025-09-08")], observed, 6)
        for f in fs:
            if f.covered is not None:
                self.assertEqual(f.high_minutes, f.active_cases * 90)

    def test_rolling_cutoffs_only_use_known_credentials(self):
        # 最早截点之后才 known_at 的事件不应进入该截点台账
        cuts = self._cuts("山前", self.date("2026-01-18"))
        self.assertGreaterEqual(len(cuts), 4)
        for r in cuts:
            fs = backtest_combo(self.bundle.past, "山前", 1, 45, [r],
                                self.date("2026-01-18"), 6)
            self.assertTrue(all(f.cutoff == r for f in fs))
