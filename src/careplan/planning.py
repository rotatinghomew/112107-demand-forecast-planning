"""新一期服务的可承诺余额、审批与回看兑现。

审批纪律（对应业务约定）：
- 必须整段服务期每周都放得下，不能只保证启动周；
- 硬余额只承认确定腾退与已签确定替代班；预测续约/回归风险只产生风险扣减；
- 借用保留固定班的新服务，仅当存在确定替代班且替代周容量可核时接受；
  没有确定替代，不得拿预测低回归率承诺固定班；
- 采购报价须未过期、总费用不超 max_cost_cents、未备基金风险不超 max_unfunded_risk；
- 放行一期后跨周占用写入登记簿，下一次计划必须反映；
- 家庭提前回来：先核原权益，再判临时方案能否整体转到已可用替代班，
  禁止为凑总工时静默取消一周。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, asdict

from .capacity import (
    FlexRegistry, ShiftCalendar, evaluate_horizon, offer_covers, offer_live,
)
from .history import ComboEstimate
from .sources import Offer
from .timeline import Ledger, week_start
from .timeutils import iter_weeks, weeks_between


@dataclass
class ProgramRequest:
    ref: str
    station: str
    start: dt.date
    weeks: int
    frequency: int
    minutes: int

    @property
    def weekly_minutes(self) -> int:
        return self.frequency * self.minutes


@dataclass
class WeekDecision:
    week: dt.date
    hard_balance: int
    risk_capped_balance: int
    need: int
    covered_by: dict[str, int]          # 来源 -> 分钟（腾退/替代/采购）
    feasible: bool
    blockers: tuple[str, ...]


@dataclass
class ApprovalDecision:
    request_ref: str
    approved: bool
    as_of: dt.date
    risk_rate_used: float | None
    combo_estimable: bool
    week_decisions: list[WeekDecision]
    offers_used: tuple[str, ...]
    total_expense_cents: int
    blockers: tuple[str, ...]
    rationale: tuple[str, ...]
    decision_id: str = field(default_factory=lambda: "D-" + uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("as_of",):
            d[key] = self.as_of.isoformat()
        for wd in d["week_decisions"]:
            wd["week"] = wd["week"].isoformat()
        return d


def _risk_rate(est: ComboEstimate | None) -> tuple[float, bool]:
    """返回用于风险扣减的比例与组合是否可校验。

    可校验组合：取续约率区间上沿与强退率上沿的综合不确定性；
    不可校验组合：风险扣减按 100% 处理（即不允许用预测释放固定班）。
    """
    if est is None or not est.estimable:
        return 1.0, False
    # 预测的“老服务仍占用”上沿 = 续约上沿 + 强退风险上沿（保守叠加，封顶 1）
    return min(1.0, (est.renew_high or 0.0) + est.relocate_high), True


def approve_program(
    req: ProgramRequest,
    as_of: dt.date,
    ledger: Ledger,
    cal: ShiftCalendar,
    flex: FlexRegistry,
    offers: list[Offer],
    estimates: dict[tuple[str, int, int], ComboEstimate],
    max_unfunded_risk: float,
    max_cost_cents: int,
    prior_commitments: dict[dt.date, int] | None = None,
) -> ApprovalDecision:
    prior_commitments = prior_commitments or {}
    horizon = list(iter_weeks(week_start(req.start), req.weeks))
    balances = evaluate_horizon(horizon, ledger, cal, flex, offers, as_of)
    est = estimates.get((req.station, req.frequency, req.minutes))
    risk_rate, estimable = _risk_rate(est)

    blockers: list[str] = []
    rationale: list[str] = []
    if not estimable:
        rationale.append("该站点×频率组合无可校验老批次，预测不得替代家庭权利；仅按确定凭据审批")
    else:
        rationale.append(
            f"组合可校验；风险上沿（续约上沿+强退上沿）={risk_rate:.3f}"
        )

    # 待续约家庭：未到复评结论不能当明示续约，也不能当释放，按硬余额保留
    chosen_offers: list[Offer] = []
    week_decisions: list[WeekDecision] = []
    total_expense = 0

    for bal in balances:
        need = req.weekly_minutes
        prior = prior_commitments.get(bal.week, 0)
        covered: dict[str, int] = {}
        week_blockers: list[str] = []

        hard = bal.hard_committable - prior
        if hard < 0:
            hard = 0
            week_blockers.append("既有审批的跨周占用已超过硬余额")

        # 风险调整：对“预测可能腾退”的部分按 risk_rate 扣减；
        # 硬余额本身全是确定凭据，因此风险扣减只作用于可选采购之外的预测空间，
        # 这里把待续约家庭占用按风险上沿冻结为不可动用。
        frozen_risk = round(
            sum(
                _pending_minutes(ledger, bal.week, c)
                for c in bal.pending_renewal_cases
            ) * risk_rate
        )
        risk_balance = max(0, hard - frozen_risk)

        remaining = need
        if risk_balance >= remaining:
            covered["hard_committable"] = remaining
            remaining = 0
        else:
            if risk_balance > 0:
                covered["hard_committable"] = risk_balance
                remaining -= risk_balance
            # 采购补齐：只用未过期且覆盖当周的报价
            for o in offers:
                if remaining <= 0:
                    break
                if not offer_live(o, as_of) or not offer_covers(o, bal.week):
                    continue
                take = min(remaining, o.weekly_minutes)
                covered[f"purchase:{o.offer}"] = (
                    covered.get(f"purchase:{o.offer}", 0) + take
                )
                remaining -= take
                if o not in chosen_offers:
                    chosen_offers.append(o)

        feasible = remaining == 0
        if not feasible:
            week_blockers.append(
                f"{bal.week.isoformat()} 仍缺 {remaining} 分钟（硬余额 {hard}，风险冻结 {frozen_risk}）"
            )

        week_decisions.append(
            WeekDecision(
                week=bal.week, hard_balance=hard, risk_capped_balance=risk_balance,
                need=need, covered_by=covered, feasible=feasible,
                blockers=tuple(week_blockers),
            )
        )
        blockers.extend(week_blockers)

    total_expense = sum(
        o.expense_cents for o in chosen_offers
    )
    if total_expense > max_cost_cents:
        blockers.append(f"采购费用 {total_expense} 超 max_cost_cents {max_cost_cents}")

    # 未备基金风险：采购中无法由确定腾退覆盖的比例
    total_need = req.weekly_minutes * req.weeks
    purchased_total = sum(
        sum(v for k, v in wd.covered_by.items() if k.startswith("purchase:"))
        for wd in week_decisions
    )
    unfunded = purchased_total / total_need if total_need else 0.0
    if unfunded > max_unfunded_risk:
        blockers.append(
            f"未备基金/采购占比 {unfunded:.3f} 超 max_unfunded_risk {max_unfunded_risk:.3f}"
        )

    # 借用保留班必须有确定替代
    for a in flex.in_week(horizon[0]):
        if not a.substitute:
            blockers.append(f"复用约 {a.document} 借用 {a.borrowed_shift} 但无确定替代班")

    approved = not blockers
    if approved:
        rationale.append(
            f"整段 {req.weeks} 周均可覆盖；采购 {[o.offer for o in chosen_offers]}，"
            f"费用 {total_expense} 分，采购占比 {unfunded:.3f}"
        )
    return ApprovalDecision(
        request_ref=req.ref, approved=approved, as_of=as_of,
        risk_rate_used=risk_rate, combo_estimable=estimable,
        week_decisions=week_decisions, offers_used=tuple(o.offer for o in chosen_offers),
        total_expense_cents=total_expense, blockers=tuple(blockers),
        rationale=tuple(rationale),
    )


def _pending_minutes(ledger: Ledger, week: dt.date, case: str) -> int:
    tl = ledger.cases.get(case)
    if not tl or week not in tl.weeks:
        return 0
    rec = tl.weeks[week]
    return rec.frequency * rec.minutes


def early_return_replan(
    case: str,
    return_week: dt.date,
    ledger: Ledger,
    cal: ShiftCalendar,
    flex: FlexRegistry,
    active_borrowers: dict[str, str],   # borrowed_shift -> 借用家庭/项目 ref
) -> dict:
    """家庭提前回来时的处置判定。

    第一步核定原有权益（原保留班、频率、分钟）；
    第二步判断临时借用方案能否整体转到已可用替代班——必须覆盖剩余全部周，
    任一周替代班无容量即整体不可转，不得静默取消一周。
    """
    tl = ledger.cases.get(case)
    if tl is None or return_week not in tl.weeks:
        return {"case": case, "transferable": False,
                "blockers": ("截点下无该家庭当周凭据",)}
    rec = tl.weeks[return_week]
    rights = {
        "case": case, "week": return_week.isoformat(),
        "reserved_shift": rec.reserved_shift,
        "frequency": rec.frequency, "minutes": rec.minutes,
        "state": rec.state.value,
    }
    blockers: list[str] = []
    plan: list[dict] = []
    for a in flex.for_case(case):
        if not flex.applies(a, return_week) and a.start > return_week:
            continue
        if not a.substitute:
            blockers.append(f"{a.document}: 无确定替代班，不能外借保留班")
            continue
        for wk in iter_weeks(max(a.start, return_week), a.weeks - weeks_between(a.start, max(a.start, return_week))):
            cap = cal.minutes(a.substitute, wk)
            ok = cap >= a.weekly_minutes
            plan.append({
                "week": wk.isoformat(), "substitute": a.substitute,
                "weekly_minutes": a.weekly_minutes, "available": cap, "ok": ok,
            })
            if not ok:
                blockers.append(
                    f"{wk.isoformat()} 替代班 {a.substitute} 容量 {cap} < {a.weekly_minutes}"
                )
    transferable = not blockers and bool(plan)
    return {
        "case": case, "rights": rights, "transferable": transferable,
        "transfer_plan": plan, "blockers": tuple(blockers),
    }


def attribute_losses(planned: dict[dt.date, int], actual: dict[dt.date, dict]) -> list[dict]:
    """把实际安排与原计划的差异归到三类原因：
    renewal_gap（续约偏差）、family_return（家庭实际恢复）、substitute_default（替代班未兑现）。
    actual[week] 形如 {"minutes": int, "reasons": {reason: minutes}}。
    """
    rows = []
    for wk in sorted(set(planned) | set(actual)):
        plan_min = planned.get(wk, 0)
        act = actual.get(wk, {"minutes": plan_min, "reasons": {}})
        gap = plan_min - act.get("minutes", plan_min)
        rows.append({
            "week": wk.isoformat(),
            "planned_minutes": plan_min,
            "actual_minutes": act.get("minutes", plan_min),
            "loss_minutes": max(0, gap),
            "attribution": act.get("reasons", {}),
        })
    return rows
