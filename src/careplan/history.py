"""历史团组的删失感知估计。

业务红线：
- 观察期结束（右删失）不等于结项；待续约未给结论不能当明示续约。
- 强退结项（relocated）走暴露人周风险率；到期不续约（nonrenewal）走续约二项比例，
  两者不允许合并成同一个比例外推。
- 站点×服务频率组合必须同时满足团组数、结局周数、滚动区间覆盖率才可校验，
  否则该组合标记 not_estimable，运营侧不得据其预测放固定班。

估计量：
- 续约率 p_renew = accepted / (accepted + nonrenewal)，Wilson 95% 区间；
- 强退周风险率 h = relocated 事件数 / 暴露人周（CLOSED 当周之后不再暴露）；
- 逐周劳时点估计与区间由 backtest 用滚动截点复核覆盖率。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .sources import Cutoff, PastEvent
from .timeline import WeekState, build_history_ledger, cohort_key, week_start
from .timeutils import weeks_between

@dataclass
class ComboEstimate:
    station: str
    frequency: int
    minutes: int
    cohorts: tuple[str, ...]
    starts: int
    renew_accepted: int
    nonrenewal: int
    censored_renewals: int
    relocated: int
    person_weeks: int
    renew_rate: float | None
    renew_low: float | None
    renew_high: float | None
    relocate_hazard: float
    relocate_low: float
    relocate_high: float
    estimable: bool
    reason: str = ""

    @property
    def combo(self) -> tuple[int, int]:
        return (self.frequency, self.minutes)


@dataclass
class CohortFacts:
    cohort: str
    station: str
    frequency: int
    minutes: int
    start_week: dt.date
    size: int
    observed_weeks: int
    active_weeks: int
    paused_weeks: int
    closed_relocated: int
    closed_nonrenewal: int
    renew_accepted: int
    censored_at_cutoff: int


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float, float]:
    if trials == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def poisson_interval(events: int, exposure: int, z: float = 1.96) -> tuple[float, float, float]:
    """暴露人周上的事件风险率，事件数按 Poisson 近似给区间。"""
    rate = events / exposure if exposure else float("nan")
    if exposure == 0:
        return rate, 0.0, float("inf")
    half = z * math.sqrt(events) / exposure if events > 0 else z / exposure
    return rate, max(0.0, rate - half), rate + half + (z / exposure if events == 0 else 0.0)


def cohort_facts(
    past: list[PastEvent], station: str, cutoff: dt.date
) -> list[CohortFacts]:
    # 截点可能落在周日；观察长度按其所在业务周的周一计
    cutoff_week = week_start(cutoff)
    ledger = build_history_ledger(past, cutoff, station)
    events_by_case: dict[str, list[PastEvent]] = {}
    for ev in past:
        if ev.station == station and ev.known_at <= cutoff and ev.effective <= cutoff:
            events_by_case.setdefault(ev.case, []).append(ev)

    out: list[CohortFacts] = []
    buckets: dict[tuple[str, int, int], list[str]] = {}
    for case, tl in ledger.cases.items():
        span = tl.horizon()
        if span is None:
            continue
        any_rec = next(iter(tl.weeks.values()))
        key = (cohort_key(case), any_rec.frequency, any_rec.minutes)
        buckets.setdefault(key, []).append(case)

    for (coh, freq, minutes), cases in sorted(buckets.items()):
        start_week = min(ledger.cases[c].horizon()[0] for c in cases)
        size = len(cases)
        observed = active_w = paused_w = 0
        n_rel = n_non = n_renew = n_cens = 0
        for c in cases:
            tl = ledger.cases[c]
            last_kind = tl.weeks[tl.horizon()[1]].events[-1]
            for wk, rec in tl.weeks.items():
                observed += 1
                if rec.state is WeekState.ACTIVE:
                    active_w += 1
                elif rec.state is WeekState.PAUSED:
                    paused_w += 1
            reasons = [ev.reason for ev in events_by_case[c]]
            kinds = [ev.kind for ev in events_by_case[c]]
            if "close" in kinds:
                if "relocated" in reasons:
                    n_rel += 1
                if "nonrenewal" in reasons:
                    n_non += 1
            n_renew += kinds.count("renew")
            if last_kind in ("due_renewal", "start", "pause"):
                n_cens += 1
        out.append(
            CohortFacts(
                cohort=coh, station=station, frequency=freq, minutes=minutes,
                start_week=start_week, size=size,
                observed_weeks=weeks_between(start_week, cutoff_week) + 1,
                active_weeks=active_w, paused_weeks=paused_w,
                closed_relocated=n_rel, closed_nonrenewal=n_non,
                renew_accepted=n_renew, censored_at_cutoff=n_cens,
            )
        )
    return out


def estimate_combos(
    past: list[PastEvent],
    cutoffs: list[Cutoff],
    policy,
    coverage_results: dict[tuple[str, int, int], float] | None = None,
) -> list[ComboEstimate]:
    coverage_results = coverage_results or {}
    by_station = {c.station: c.observed_through for c in cutoffs}
    result: list[ComboEstimate] = []

    stations = sorted({e.station for e in past})
    for station in stations:
        cutoff = by_station[station]
        facts = cohort_facts(past, station, cutoff)
        combos = sorted({(f.frequency, f.minutes) for f in facts})
        for freq, minutes in combos:
            fs_all = [f for f in facts if f.frequency == freq and f.minutes == minutes]
            fs = [f for f in fs_all if f.observed_weeks >= policy.min_outcome_weeks] \
                if policy and station == policy.station else fs_all
            cohorts = tuple(sorted({f.cohort for f in fs}))
            starts = sum(f.size for f in fs)
            n_renew = sum(f.renew_accepted for f in fs)
            n_non = sum(f.closed_nonrenewal for f in fs)
            n_rel = sum(f.closed_relocated for f in fs)
            n_cens_dr = sum(f.censored_at_cutoff for f in fs)
            person_weeks = sum(f.active_weeks + f.paused_weeks for f in fs)

            r_trials = n_renew + n_non
            rate, lo, hi = wilson_interval(n_renew, r_trials)
            h, hlo, hhi = poisson_interval(n_rel, person_weeks)

            cov = coverage_results.get((station, freq, minutes))
            ok, why = _check_estimable(policy, station, fs, cov)
            result.append(
                ComboEstimate(
                    station=station, frequency=freq, minutes=minutes,
                    cohorts=cohorts, starts=starts,
                    renew_accepted=n_renew, nonrenewal=n_non,
                    censored_renewals=n_cens_dr, relocated=n_rel,
                    person_weeks=person_weeks,
                    renew_rate=None if r_trials == 0 else rate,
                    renew_low=None if r_trials == 0 else lo,
                    renew_high=None if r_trials == 0 else hi,
                    relocate_hazard=h, relocate_low=hlo, relocate_high=hhi,
                    estimable=ok, reason=why,
                )
            )
    return result


def _check_estimable(policy, station: str, facts: list[CohortFacts],
                     coverage: float | None) -> tuple[bool, str]:
    if policy is None or station != policy.station:
        return False, "该站点无计划参数，全部组合不可校验"
    # 只有观察长度达到 min_outcome_weeks 的团组才有资格计入团组门槛
    qualified = [f for f in facts if f.observed_weeks >= policy.min_outcome_weeks]
    if len(qualified) < policy.min_cohorts:
        return False, (
            f"满足 min_outcome_weeks={policy.min_outcome_weeks} 的团组仅 "
            f"{len(qualified)} 个 < min_cohorts {policy.min_cohorts}"
        )
    if coverage is None:
        return False, "尚无滚动截点区间覆盖率"
    if coverage < policy.min_interval_coverage:
        return False, f"区间覆盖率 {coverage:.2f} < {policy.min_interval_coverage:.2f}"
    return True, ""
