"""审批引擎：整周期覆盖、硬余额纪律、替代班约束、采购限额、跨周结转。"""

import datetime as dt
import tempfile
from pathlib import Path

from _base import CarePlanTestCase
from careplan.capacity import FlexRegistry, ShiftCalendar
from careplan.history import estimate_combos
from careplan.planning import (
    ProgramRequest, approve_program, early_return_replan,
)
from careplan.registry import Registry
from careplan.timeline import build_ledger, extend_horizon
from careplan.timeutils import add_weeks


class PlanningTests(CarePlanTestCase):
    def setUp(self):
        self.as_of = self.date("2026-05-15")
        self.station = "山前"
        self.ledger = build_ledger(self.bundle.accounts, self.as_of, self.station)
        extend_horizon(self.ledger, self.date("2026-06-08"))
        self.cal = ShiftCalendar.from_rows(self.bundle.shifts, self.station, self.as_of)
        self.flex = FlexRegistry(self.bundle.flex)
        self.offers = [o for o in self.bundle.offers if o.station == self.station]
        self.policy = self.bundle.policy_for(self.station)
        coverage = {("山前", f, m): 1.0 for f in (1, 2) for m in (45, 60)}
        self.estimates = {
            (e.station, e.frequency, e.minutes): e
            for e in estimate_combos(self.bundle.past, self.bundle.cutoffs,
                                     self.policy, coverage)
        }

    def _approve(self, ref, weeks=2, freq=1, minutes=60, frm="2026-05-18", prior=None):
        req = ProgramRequest(ref, self.station, self.date(frm), weeks, freq, minutes)
        return approve_program(
            req, self.as_of, self.ledger, self.cal, self.flex, self.offers,
            self.estimates, self.policy.max_unfunded_risk,
            self.policy.max_cost_cents, prior or {},
        )

    def test_full_horizon_must_be_covered_not_just_start_week(self):
        # 启动周放得下、但末周硬余额为 0 的长周期不能仅靠启动周通过：
        # 末周只能依赖采购，整体因采购占比/费用超限被拦
        d = self._approve("LONG", weeks=4, freq=2, minutes=60)
        self.assertFalse(d.approved)
        last_week = d.week_decisions[-1]
        self.assertNotIn("hard_committable", last_week.covered_by)
        self.assertTrue(
            any(k.startswith("purchase:") for k in last_week.covered_by)
        )
        self.assertTrue(
            any("未备基金" in b or "费用" in b for b in d.blockers)
        )

    def test_small_request_within_hard_balance_approved(self):
        d = self._approve("OK", weeks=2, freq=1, minutes=60)
        self.assertTrue(d.approved, d.blockers)
        self.assertTrue(all(w.feasible for w in d.week_decisions))
        self.assertEqual(d.offers_used, ())

    def test_purchase_ratio_over_unfunded_limit_blocks(self):
        d = self._approve("RISK", weeks=3, freq=2, minutes=60)
        self.assertFalse(d.approved)
        self.assertTrue(any("未备基金" in b for b in d.blockers))

    def test_expired_offer_not_used(self):
        # 5/21 时 O1 已过期（expires 5/15）
        as_of = self.date("2026-05-21")
        ledger = build_ledger(self.bundle.accounts, as_of, self.station)
        extend_horizon(ledger, self.date("2026-06-15"))
        cal = ShiftCalendar.from_rows(self.bundle.shifts, self.station, as_of)
        req = ProgramRequest("EXP", self.station, self.date("2026-05-25"), 4, 2, 60)
        d = approve_program(
            req, as_of, ledger, cal, self.flex, self.offers, self.estimates,
            self.policy.max_unfunded_risk, self.policy.max_cost_cents, {},
        )
        self.assertNotIn("O1", d.offers_used)

    def test_prior_commitment_carries_into_next_plan(self):
        first = self._approve("FIRST", weeks=1, freq=2, minutes=60)
        self.assertTrue(first.approved)
        prior = {self.date("2026-05-18"): 120}
        second = self._approve("SECOND", weeks=1, freq=2, minutes=60, prior=prior)
        # 已被上一期占用的余额必须从硬余额中扣减
        self.assertEqual(second.week_decisions[0].hard_balance,
                         first.week_decisions[0].hard_balance - 120)

    def test_borrowed_reserved_shift_requires_substitute(self):
        # L1 借用 K1 且有确定替代 K3；构造一份无替代的复用约应被拦
        from careplan.sources import FlexAgreement
        bad_flex = FlexRegistry([
            FlexAgreement("LX", "NEW1", self.date("2026-05-18"), 3, 120, "K1", None,
                          self.date("2026-05-12")),
        ])
        d = self._approve("NOSUB", weeks=1, freq=1, minutes=45)
        # 用坏 flex 重跑
        req = ProgramRequest("NOSUB", self.station, self.date("2026-05-18"), 1, 1, 45)
        d2 = approve_program(
            req, self.as_of, self.ledger, self.cal, bad_flex, self.offers,
            self.estimates, self.policy.max_unfunded_risk,
            self.policy.max_cost_cents, {},
        )
        self.assertFalse(d2.approved)
        self.assertTrue(any("无确定替代班" in b for b in d2.blockers))

    def test_early_return_confirms_rights_before_transfer(self):
        result = early_return_replan(
            "H1", self.date("2026-05-25"), self.ledger, self.cal, self.flex, {},
        )
        self.assertEqual(result["rights"]["reserved_shift"], "K1")
        self.assertEqual(result["rights"]["frequency"], 2)
        # H1 名下无替代约，不能静默外借其保留班
        self.assertFalse(result["transferable"])


class RegistryTests(CarePlanTestCase):
    def test_decision_versioning_and_commitment_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry(Path(tmp) / "plan.json")
            reg.record_decision({
                "request_ref": "A", "approved": True, "as_of": "2026-05-15",
                "week_decisions": [
                    {"week": "2026-05-18", "need": 60, "feasible": True},
                    {"week": "2026-05-25", "need": 60, "feasible": True},
                ],
            })
            reg.record_decision({
                "request_ref": "A", "approved": False, "as_of": "2026-05-20",
                "week_decisions": [{"week": "2026-05-18", "need": 99, "feasible": False}],
            })
            # 同 ref 追加版本而非覆盖
            self.assertEqual(len(reg.decisions_for("A")), 2)
            # 只有批准的决定形成跨周占用，被拒版本不进承诺
            commitments = reg.approved_commitments()
            self.assertEqual(commitments["2026-05-18"], 60)
            self.assertNotIn("2026-05-25", {k: v for k, v in commitments.items() if v != 60} or {})

    def test_review_records_are_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry(Path(tmp) / "plan.json")
            reg.record_review("A", [{"week": "2026-05-18"}], "第一次回看")
            reg.record_review("A", [{"week": "2026-05-25"}], "第二次回看")
            self.assertEqual(len(reg.reviews("A")), 2)
