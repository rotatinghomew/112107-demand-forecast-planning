"""滚动截点回验：用只包含当时已知凭据的数据预测未来各周劳时区间，
再与后来真正发生、且在站点观察止日内可见的回执核对覆盖率。

规则：
- 每个回验截点 r 只使用 known_at <= r 且 effective <= r 的凭据；
- 预测窗口为 r 之后的 min_outcome_weeks 周；
- 某预测周若在站点观察止日所在周之后，没有可核真值，计为 uncheckable 而非失败；
- 区间按“团组×周龄”的活跃占比经验分位数构造：对每个在 r 时点仍在户的团组，
  取同组合历史团组在该周龄（无同龄样本时回退最近已观察周龄）的活跃占比 10%/90% 分位，
  乘以该团组起始规模与单户周分钟，再对团组求和；
- 右删失团组仍作为在户团组参与预测，不被当作已结项；
- 样本团组少于 2 个的预测周不可校验。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .sources import PastEvent
from .timeline import (
    WeekState, build_history_ledger, cohort_key, extend_horizon, week_start,
)
from .timeutils import add_weeks, iter_weeks, weeks_between


@dataclass
class WeekForecast:
    station: str
    frequency: int
    minutes: int
    cutoff: dt.date
    target_week: dt.date
    active_cases: int
    low_minutes: int
    point_minutes: int
    high_minutes: int
    actual_minutes: int | None
    covered: bool | None          # None = 不可校验
    reason: str = ""


@dataclass
class CoverageSummary:
    station: str
    frequency: int
    minutes: int
    checkable: int
    covered: int
    uncheckable: int

    @property
    def coverage(self) -> float | None:
        return self.covered / self.checkable if self.checkable else None


def _wilson(active: int, size: int, z: float = 1.96) -> tuple[float, float]:
    """单团组在某周龄活跃占比的 Wilson 区间，刻画团组内抽样不确定性。"""
    if size == 0:
        return 0.0, 1.0
    p = active / size
    denom = 1 + z * z / size
    center = (p + z * z / (2 * size)) / denom
    half = z * (p * (1 - p) / size + z * z / (4 * size * size)) ** 0.5 / denom
    return max(0.0, center - half), min(1.0, center + half)


def rolling_cutoffs(first: dt.date, last: dt.date, step_weeks: int = 4) -> list[dt.date]:
    out = []
    cur = first
    while cur <= last:
        out.append(cur)
        cur = add_weeks(cur, step_weeks)
    return out


def _combo_starts(past, station, freq, minutes) -> dict[str, dt.date]:
    starts: dict[str, dt.date] = {}
    for ev in past:
        if (ev.station == station and ev.kind == "start"
                and ev.frequency == freq and ev.minutes == minutes):
            starts.setdefault(ev.case, ev.effective)
    return starts


def _age_pool(past, station, cutoff, freq, minutes):
    """历史团组在各周龄的汇总池：{age: (活跃户数, 在册户数)}。

    在册户数包含 ACTIVE/PAUSED（仍占用或保留权益），CLOSED 已离开该周池。
    同周龄跨团组的活跃率在数据中高度一致，因此直接按周龄汇总，
    抽样不确定性交给预测端对在户团组做 Wilson 区间。
    """
    ledger = build_history_ledger(past, cutoff, station)
    starts = _combo_starts(past, station, freq, minutes)
    pool: dict[int, list[int]] = {}
    for case, tl in ledger.cases.items():
        start = starts.get(case)
        if start is None:
            continue
        rec0 = next(iter(tl.weeks.values()))
        if (rec0.frequency, rec0.minutes) != (freq, minutes):
            continue
        for wk, rec in tl.weeks.items():
            age = weeks_between(start, wk)
            cell = pool.setdefault(age, [0, 0])
            cell[1] += 1
            if rec.state == WeekState.ACTIVE:
                cell[0] += 1
    return pool


def _worst_step_drop(pool: dict[int, list[int]]) -> float:
    """历史相邻周龄间的最大单步降幅（按活跃率差），用于外推下界。

    样本不足时给保守默认 0.25。
    """
    ages = sorted(pool)
    drops: list[float] = []
    for a, b in zip(ages, ages[1:]):
        pa = pool[a][0] / pool[a][1]
        pb = pool[b][0] / pool[b][1]
        if pb < pa:
            drops.append(pa - pb)
    return max(drops, default=0.25)


def backtest_combo(
    past: list[PastEvent],
    station: str,
    frequency: int,
    minutes: int,
    cutoffs: list[dt.date],
    observed_through: dt.date,
    horizon_weeks: int,
) -> list[WeekForecast]:
    forecasts: list[WeekForecast] = []
    observed_week = week_start(observed_through)
    per_case_week = frequency * minutes

    for r in cutoffs:
        pool = _age_pool(past, station, r, frequency, minutes)
        starts = _combo_starts(past, station, frequency, minutes)
        ledger_r = build_history_ledger(past, r, station)
        ledger_actual = build_history_ledger(past, observed_through, station)
        # 右删失家庭的最后已知状态延续到观察止日，目标周才能取到真值
        extend_horizon(ledger_actual, observed_week)
        extend_horizon(ledger_r, min(r, observed_week))
        r_week = week_start(r)
        known_ages = sorted(pool)
        max_drop = _worst_step_drop(pool)

        # r 时点仍在户的团组（右删失也算在户）：起始规模、当前活跃户数与起始周
        # 仅纳入 start 已生效的团组；start 晚于 r 的团组当时尚不存在
        alive: dict[str, dict] = {}
        for c, tl in ledger_r.cases.items():
            if c not in starts or not tl.horizon() or starts[c] > r_week:
                continue
            if tl.weeks[tl.horizon()[1]].state == WeekState.CLOSED:
                continue
            coh = cohort_key(c)
            rec = tl.weeks.get(r_week) or tl.weeks[tl.horizon()[1]]
            slot = alive.setdefault(coh, {"size": 0, "active": 0, "age0": starts[c]})
            slot["size"] += 1
            if rec.state == WeekState.ACTIVE:
                slot["active"] += 1

        for target in iter_weeks(add_weeks(r, 1), horizon_weeks):
            low_total = point_total = high_total = 0.0
            supported = True
            extrapolated = False
            active_count = sum(s["size"] for s in alive.values())
            for coh, slot in alive.items():
                target_age = weeks_between(slot["age0"], target)
                anchor_share = slot["active"] / slot["size"]
                a_lo, _ = _wilson(slot["active"], slot["size"])
                full_minutes = slot["size"] * per_case_week
                if target_age in pool:
                    # 有同龄历史：点估计取当前锚定率与历史同龄池的较高者（存活者更健康）；
                    # 下界从锚定 Wilson 下界再允许一次历史最陡单步降幅；
                    # 上界为满负荷——暂停家庭持回归权，可在任一周全部恢复
                    n_active, n_booked = pool[target_age]
                    if n_booked < 2:
                        supported = False
                        break
                    hist_point = n_active / n_booked
                    share_point = max(anchor_share, hist_point)
                    share_lo = max(0.0, a_lo - max_drop)
                else:
                    # 无同龄历史：以当前实际活跃率为点估计，
                    # 下界按历史最陡单步降幅累计，上界仍为满负荷
                    younger = [a for a in known_ages if a < target_age]
                    if not younger:
                        supported = False
                        break
                    steps = target_age - younger[-1]
                    share_point = anchor_share
                    share_lo = max(0.0, a_lo - max_drop * steps)
                    extrapolated = True
                low_total += share_lo * full_minutes
                high_total += full_minutes
                point_total += share_point * full_minutes

            actual = None
            covered: bool | None = None
            reason = ""
            if target > observed_week:
                reason = "目标周超出观察止日，不可校验"
            elif not supported or active_count == 0:
                reason = "同周龄历史批次样本不足（少于2个团组）"
            else:
                # 真值只核对截点 r 时已 start 的在户团组；
                # r 之后才开的新团组属于后续计划对象，不进本次预测核对
                alive_cases = {
                    c for c, tl in ledger_r.cases.items()
                    if c in starts and starts[c] <= r_week
                    and tl.horizon()
                    and tl.weeks[tl.horizon()[1]].state != WeekState.CLOSED
                }
                actual = sum(
                    rec.weekly_minutes
                    for c, tl in ledger_actual.cases.items()
                    if c in alive_cases and (rec := tl.weeks.get(target)) is not None
                )
                covered = low_total <= actual <= high_total
                if extrapolated:
                    reason = "高龄周样本由更年轻周龄外推"

            forecasts.append(
                WeekForecast(
                    station=station, frequency=frequency, minutes=minutes,
                    cutoff=r, target_week=target, active_cases=active_count,
                    low_minutes=int(low_total), point_minutes=int(point_total),
                    high_minutes=int(high_total),
                    actual_minutes=actual, covered=covered, reason=reason,
                )
            )
    return forecasts


def summarize_coverage(forecasts: list[WeekForecast]) -> list[CoverageSummary]:
    groups: dict[tuple[str, int, int], list[WeekForecast]] = {}
    for f in forecasts:
        groups.setdefault((f.station, f.frequency, f.minutes), []).append(f)
    out = []
    for (station, freq, minutes), fs in sorted(groups.items()):
        checkable = [f for f in fs if f.covered is not None]
        out.append(
            CoverageSummary(
                station=station, frequency=freq, minutes=minutes,
                checkable=len(checkable),
                covered=sum(1 for f in checkable if f.covered),
                uncheckable=sum(1 for f in fs if f.covered is None),
            )
        )
    return out
