"""凭据事件溯源：把逐户凭据重放成逐周状态。

三种周状态：
- ACTIVE  当周有上门义务，固定班被该户占用（临时离家也保留回归权益，但不计上门）
- PAUSED  当周无上门回执，但保留原班与优先回归权（return_right）
- CLOSED  已结项，班可腾退

关键纪律：
1. 只重放 ``known_at <= as_of`` 的凭据——通知时间晚于计划截点的凭据不得提前使用。
2. 右删失（观察期结束仍 ACTIVE/PAUSED/待续约）不是结项。
3. 同一生效周内按 (effective, known_at, 事件序号) 排序，保证同周先停后复等顺序确定。
4. 强退结项（relocated）与到期不续约（nonrenewal）只是 close 的不同 reason，
   状态上同样释放班次，但在历史估计里走不同风险模型（见 history.py）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

from .sources import AccountEvent, PastEvent
from .timeutils import add_weeks, week_start


class WeekState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


# 状态机：当前态 -> 允许接收的事件 kind
_TRANSITIONS = {
    None: {"start"},
    WeekState.ACTIVE: {"pause", "resume", "close", "due_renewal", "renew"},
    WeekState.PAUSED: {"resume", "close", "due_renewal", "renew"},
    WeekState.CLOSED: {"start"},
}


@dataclass
class CaseWeek:
    case: str
    week: dt.date
    state: WeekState
    frequency: int
    minutes: int
    reserved_shift: str | None
    reason: str | None = None
    events: tuple[str, ...] = ()

    @property
    def weekly_minutes(self) -> int:
        """当周承诺占用分钟：只有 ACTIVE 计上门工时；暂停周为 0 但保留权益。"""
        return self.frequency * self.minutes if self.state is WeekState.ACTIVE else 0

    @property
    def reserved_minutes(self) -> int:
        """暂停周仍被保留、不能腾退给他人的分钟。"""
        return self.frequency * self.minutes if self.state is WeekState.PAUSED else 0


@dataclass
class CaseTimeline:
    case: str
    station: str
    weeks: dict[dt.date, CaseWeek] = field(default_factory=dict)
    reserved_shift: str | None = None

    def state_at(self, week: dt.date) -> WeekState:
        rec = self.weeks.get(week)
        return rec.state if rec else WeekState.CLOSED

    def horizon(self) -> tuple[dt.date, dt.date] | None:
        if not self.weeks:
            return None
        keys = sorted(self.weeks)
        return keys[0], keys[-1]


@dataclass
class Ledger:
    """某一知情截点下、全部家庭的逐周视图。"""

    as_of: dt.date
    station: str
    cases: dict[str, CaseTimeline]

    def weeks(self) -> list[dt.date]:
        keys = set()
        for tl in self.cases.values():
            keys.update(tl.weeks)
        return sorted(keys)

    def records_in(self, week: dt.date) -> list[CaseWeek]:
        return [tl.weeks[week] for tl in self.cases.values() if week in tl.weeks]

    def occupied_minutes(self, week: dt.date) -> int:
        return sum(r.weekly_minutes for r in self.records_in(week))

    def held_minutes(self, week: dt.date) -> int:
        """暂停但保留回归权的分钟（不可腾退）。"""
        return sum(r.reserved_minutes for r in self.records_in(week))

    def releasable_minutes(self, week: dt.date) -> int:
        """已 CLOSED 且其原班在该周不再被任何凭据保留的分钟。

        注意：仅当没有任何 ACTIVE/PAUSED 记录仍挂在同一保留班时才算腾退；
        这里按家庭维度汇总，逐班核对在 capacity.planning 层完成。
        """
        return sum(
            r.frequency * r.minutes
            for r in self.records_in(week)
            if r.state is WeekState.CLOSED
        )

    def pending_renewals(self, week: dt.date) -> list[CaseWeek]:
        """到 as_of 为止只收到 due_renewal、尚无续约结论的家庭（明示续约未到）。"""
        return [
            r for r in self.records_in(week)
            if r.state in (WeekState.ACTIVE, WeekState.PAUSED)
            and "due_renewal" in r.events and "renew" not in r.events
        ]


def _keyed(event) -> tuple:
    return (week_start(event.effective), event.known_at, event.event)


def build_case_timeline(case: str, station: str, events: list, as_of: dt.date) -> CaseTimeline:
    """重放单户凭据。events 已按事件自然顺序给出，函数内部再做稳定排序。"""
    visible = [e for e in events if e.known_at <= as_of]
    visible.sort(key=_keyed)

    tl = CaseTimeline(case=case, station=station)
    if not visible:
        return tl

    state: WeekState | None = None
    freq = minutes = 0
    shift: str | None = None
    reason: str | None = None
    fired: list[str] = []
    current_week: dt.date | None = None

    def carry(week: dt.date) -> None:
        nonlocal current_week
        if current_week is None:
            current_week = week
        while current_week < week:
            tl.weeks[current_week] = CaseWeek(
                case, current_week, state, freq, minutes, shift, reason, tuple(fired)
            )
            current_week = add_weeks(current_week, 1)

    for e in visible:
        wk = week_start(e.effective)
        if current_week is None:
            current_week = wk
        elif wk > current_week:
            carry(wk)
        if e.kind not in _TRANSITIONS[state]:
            raise ValueError(
                f"家庭 {case} 凭据 {e.event}: 状态 {state} 不接受事件 {e.kind}"
            )
        fired.append(e.kind)
        reason = e.reason
        if e.kind == "start":
            state = WeekState.ACTIVE
            freq, minutes = e.frequency, e.minutes
            shift = getattr(e, "reserved_shift", None)
        elif e.kind == "pause":
            state = WeekState.PAUSED
            freq, minutes = e.frequency, e.minutes
        elif e.kind == "resume":
            if reason != "return_right":
                raise ValueError(f"家庭 {case} 恢复凭据 {e.event} 缺少 return_right 依据")
            state = WeekState.ACTIVE
        elif e.kind == "close":
            state = WeekState.CLOSED
        elif e.kind == "due_renewal":
            # 承诺仍具效力，状态不变，仅记录待续约事实
            pass
        elif e.kind == "renew":
            if reason != "accepted":
                raise ValueError(f"家庭 {case} 续约凭据 {e.event} reason 非 accepted")
            state = WeekState.ACTIVE
        tl.reserved_shift = shift
        tl.weeks[wk] = CaseWeek(case, wk, state, freq, minutes, shift, reason, tuple(fired))
        current_week = wk

    # 最后一个事件周之后不再臆造周；右删失家庭停在最后已知状态，由调用方决定视界。
    return tl


def build_ledger(events: list[AccountEvent], as_of: dt.date, station: str | None = None) -> Ledger:
    grouped: dict[str, list[AccountEvent]] = {}
    stations: dict[str, str] = {}
    for e in events:
        if station is not None and e.station != station:
            continue
        grouped.setdefault(e.case, []).append(e)
        stations[e.case] = e.station
    cases = {
        case: build_case_timeline(case, stations[case], evs, as_of)
        for case, evs in sorted(grouped.items())
    }
    st = station or next(iter(stations.values()), "")
    return Ledger(as_of=as_of, station=st, cases=cases)


def build_history_ledger(events: list[PastEvent], as_of: dt.date, station: str) -> Ledger:
    """历史回执走同一台状态机，但 PastEvent 没有 reserved_shift 字段。

    台账代表 as_of 时点的已知状态：最后已知状态正向延续到 as_of 所在周，
    右删失家庭保持原态，不转 CLOSED。
    """
    grouped: dict[str, list[PastEvent]] = {}
    for e in events:
        if e.station == station and e.known_at <= as_of and e.effective <= as_of:
            grouped.setdefault(e.case, []).append(e)
    cases = {
        case: build_case_timeline(case, station, evs, as_of)
        for case, evs in sorted(grouped.items())
    }
    ledger = Ledger(as_of=as_of, station=station, cases=cases)
    extend_horizon(ledger, week_start(as_of))
    return ledger


def extend_horizon(ledger: Ledger, through: dt.date) -> None:
    """把每个家庭的最后已知状态正向延续到 through（右删失保持原态，不转 CLOSED）。"""
    for tl in ledger.cases.values():
        span = tl.horizon()
        if span is None:
            continue
        last = span[1]
        rec = tl.weeks[last]
        cur = add_weeks(last, 1)
        while cur <= through:
            tl.weeks[cur] = CaseWeek(
                tl.case, cur, rec.state, rec.frequency, rec.minutes,
                rec.reserved_shift, rec.reason, rec.events,
            )
            cur = add_weeks(cur, 1)


def cohort_key(case_id: str) -> str:
    """历史团组按 case 前缀（开始批次）归集。"""
    return case_id.split("-", 1)[0]
