"""站点班次容量与逐周可承诺余额。

容量三个来源，互不重复、且各自需要凭据：
1. 固定班（has_fixed=yes）上现有承诺腾退出来的分钟；
2. 已签复用约（flex_agreements）指定的替代班，且只在适用周有效；
3. 已审批的跨周采购（offers），未过期且在采购覆盖周内。

纪律：
- 暂停家庭的固定班被保留（held），不能腾退、不能凭预测回归率外借；
- 借用保留固定班的新服务，仅当合同里写了确定替代班才接受；
- 预测的续约/回归风险只进风险余额，不进硬余额，更不替代家庭权利。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .sources import FlexAgreement, Offer, ShiftRow
from .timeline import Ledger, WeekState
from .timeutils import iter_weeks


@dataclass
class ShiftCalendar:
    station: str
    _by_shift_week: dict[tuple[str, dt.date], ShiftRow] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: list[ShiftRow], station: str, as_of: dt.date) -> "ShiftCalendar":
        cal = cls(station=station)
        for r in rows:
            if r.station != station:
                continue
            if r.recorded_at > as_of:
                # 计划截点之后才登记的班次容量不可用
                continue
            cal._by_shift_week[(r.shift, r.week)] = r
        return cal

    def row(self, shift: str, week: dt.date) -> ShiftRow | None:
        return self._by_shift_week.get((shift, week))

    def minutes(self, shift: str, week: dt.date) -> int:
        r = self.row(shift, week)
        return r.available_minutes if r else 0

    def is_fixed(self, shift: str, week: dt.date) -> bool:
        r = self.row(shift, week)
        return bool(r and r.has_fixed)

    def shifts_in_week(self, week: dt.date) -> list[ShiftRow]:
        return [r for (s, w), r in self._by_shift_week.items() if w == week]


@dataclass
class FlexRegistry:
    """已签复用/替代约定。borrowed_shift 是被暂缓家庭的保留班，substitute 是确定替代。"""

    agreements: list[FlexAgreement]

    def for_case(self, case: str) -> list[FlexAgreement]:
        return [a for a in self.agreements if a.case == case]

    def applies(self, a: FlexAgreement, week: dt.date) -> bool:
        weeks = set(iter_weeks(a.start, a.weeks))
        return week in weeks

    def in_week(self, week: dt.date) -> list[FlexAgreement]:
        return [a for a in self.agreements if self.applies(a, week)]


@dataclass
class WeeklyBalance:
    week: dt.date
    capacity_total: int                  # 当周班次表总分钟
    occupied_minutes: int                # ACTIVE 个别承诺占用
    held_minutes: int                    # PAUSED 保留（不可腾退）
    closed_reserved_minutes: int         # 已结项家庭原固定班的可腾退分钟
    flex_substitute_minutes: int         # 复用约里确定替代班可提供的分钟
    purchased_minutes: int               # 当周可用采购分钟（未审批前为“可选”）
    pending_renewal_cases: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def hard_committable(self) -> int:
        """硬余额：容量扣除在户占用与暂停保留，再加已签确定替代班。

        复用约借用站内固定班时，该班本就计入 capacity_total，不重复加。
        """
        return max(
            0,
            self.capacity_total - self.occupied_minutes - self.held_minutes,
        ) + self.flex_substitute_minutes


def offer_live(offer: Offer, as_of: dt.date) -> bool:
    """报价必须在计划截点前仍可接受（expires_at 为最后可签日）。"""
    return offer.expires_at >= as_of


def offer_covers(offer: Offer, week: dt.date) -> bool:
    return week in set(iter_weeks(offer.start, offer.weeks))


def weekly_capacity(cal: ShiftCalendar, week: dt.date) -> int:
    return sum(r.available_minutes for r in cal.shifts_in_week(week))


def evaluate_week(
    week: dt.date,
    ledger: Ledger,
    cal: ShiftCalendar,
    flex: FlexRegistry,
    offers: list[Offer],
    as_of: dt.date,
) -> WeeklyBalance:
    records = ledger.records_in(week)
    occupied = sum(r.weekly_minutes for r in records)
    held = sum(r.reserved_minutes for r in records)

    # 可腾退：已结项家庭原挂固定班的分钟；若该班本周仍被在户占用则不重复释放
    live_fixed_claims: dict[str, int] = {}
    for r in records:
        if r.state in (WeekState.ACTIVE, WeekState.PAUSED) and r.reserved_shift:
            live_fixed_claims[r.reserved_shift] = (
                live_fixed_claims.get(r.reserved_shift, 0) + r.frequency * r.minutes
            )
    closed_minutes = 0
    for r in records:
        if r.state is WeekState.CLOSED and r.reserved_shift:
            closed_minutes += r.frequency * r.minutes

    # 复用约替代班：必须是确定 substitute，且替代班本周确有容量
    flex_minutes = 0
    notes: list[str] = []
    for a in flex.in_week(week):
        if a.signed_at > as_of:
            notes.append(f"{a.document}: 签署晚于截点，忽略")
            continue
        if not a.substitute:
            notes.append(f"{a.document}: 无确定替代班，不能据此承诺固定班")
            continue
        cap = cal.minutes(a.substitute, week)
        if cap <= 0:
            notes.append(f"{a.document}: 替代班 {a.substitute} 当周无容量")
            continue
        flex_minutes += min(a.weekly_minutes, cap)

    purchased = 0
    for o in offers:
        if offer_live(o, as_of) and offer_covers(o, week):
            purchased += o.weekly_minutes

    pending = tuple(
        r.case for r in records
        if r.state in (WeekState.ACTIVE, WeekState.PAUSED)
        and "due_renewal" in r.events and "renew" not in r.events
    )

    return WeeklyBalance(
        week=week,
        capacity_total=weekly_capacity(cal, week),
        occupied_minutes=occupied,
        held_minutes=held,
        closed_reserved_minutes=closed_minutes,
        flex_substitute_minutes=flex_minutes,
        purchased_minutes=purchased,
        pending_renewal_cases=pending,
        notes=tuple(notes),
    )


def evaluate_horizon(
    weeks: list[dt.date],
    ledger: Ledger,
    cal: ShiftCalendar,
    flex: FlexRegistry,
    offers: list[Offer],
    as_of: dt.date,
) -> list[WeeklyBalance]:
    return [evaluate_week(w, ledger, cal, flex, offers, as_of) for w in weeks]
