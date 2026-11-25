"""事件溯源与逐周状态机的业务红线测试。"""

import datetime as dt

from _base import CarePlanTestCase
from careplan.timeline import (
    WeekState, build_case_timeline, build_ledger, extend_horizon, week_start,
)


class TimelineTests(CarePlanTestCase):
    def test_pause_holds_shift_without_service_minutes(self):
        # H1：5/4 暂停 -> 当周无上门分钟但保留 K1
        ledger = build_ledger(self.bundle.accounts, self.date("2026-05-15"), "山前")
        rec = ledger.cases["H1"].weeks[self.date("2026-05-04")]
        self.assertEqual(rec.state, WeekState.PAUSED)
        self.assertEqual(rec.weekly_minutes, 0)
        self.assertEqual(rec.reserved_minutes, 120)
        self.assertEqual(rec.reserved_shift, "K1")

    def test_resume_restores_original_shift_right(self):
        # H1 5/25 恢复，重新占用原班 K1
        ledger = build_ledger(self.bundle.accounts, self.date("2026-05-26"), "山前")
        rec = ledger.cases["H1"].weeks[self.date("2026-05-25")]
        self.assertEqual(rec.state, WeekState.ACTIVE)
        self.assertEqual(rec.reserved_shift, "K1")
        self.assertEqual(rec.weekly_minutes, 120)

    def test_known_at_cutoff_hides_future_credentials(self):
        # 5/15 时 E4（5/23 通知）不可见；5/24 后才出现待续约
        early = build_ledger(self.bundle.accounts, self.date("2026-05-15"), "山前")
        extend_horizon(early, self.date("2026-06-01"))
        self.assertNotIn("due_renewal", early.cases["H2"].weeks[self.date("2026-06-01")].events)
        later = build_ledger(self.bundle.accounts, self.date("2026-05-24"), "山前")
        self.assertIn("due_renewal", later.cases["H2"].weeks[self.date("2026-06-01")].events)

    def test_due_renewal_without_conclusion_is_not_renewal_or_release(self):
        ledger = build_ledger(self.bundle.accounts, self.date("2026-05-24"), "山前")
        rec = ledger.cases["H2"].weeks[self.date("2026-06-01")]
        self.assertEqual(rec.state, WeekState.ACTIVE)   # 承诺仍有效力
        self.assertNotIn("renew", rec.events)
        pending = ledger.pending_renewals(self.date("2026-06-01"))
        self.assertEqual([r.case for r in pending], ["H2"])

    def test_renewal_accepted_keeps_commitment(self):
        ledger = build_ledger(self.bundle.accounts, self.date("2026-06-08"), "山前")
        rec = ledger.cases["H2"].weeks[self.date("2026-06-01")]
        self.assertIn("renew", rec.events)
        self.assertEqual(rec.state, WeekState.ACTIVE)

    def test_censored_horizon_keeps_state_never_closes(self):
        from careplan.timeline import build_history_ledger
        ledger = build_history_ledger(self.bundle.past, self.date("2026-01-18"), "山前")
        # 找出截点时仍未结项（右删失）的家庭，延续后必须保持原态
        censored = [
            tl for tl in ledger.cases.values()
            if tl.weeks[tl.horizon()[1]].state != WeekState.CLOSED
        ]
        self.assertGreater(len(censored), 0)
        extend_horizon(ledger, self.date("2026-03-30"))
        for tl in censored:
            last = tl.weeks[tl.horizon()[1]]
            self.assertNotEqual(last.state, WeekState.CLOSED)

    def test_invalid_transition_raises(self):
        from careplan.sources import AccountEvent
        bad = [
            AccountEvent("X1", "C", "山前", self.date("2026-05-04"), self.date("2026-05-01"),
                         "resume", 2, 60, "K1", "return_right"),
        ]
        with self.assertRaises(ValueError):
            build_case_timeline("C", "山前", bad, self.date("2026-05-15"))
