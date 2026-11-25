"""命令行入口：

python3 -m careplan.cli ledger     --data data --as-of 2026-05-15 [--station 山前]
python3 -m careplan.cli history    --data data [--station 山前]
python3 -m careplan.cli backtest   --data data [--station 山前]
python3 -m careplan.cli approve    --data data --ref N1 --from 2026-05-18 --weeks 3 \
                                   --frequency 2 --minutes 60 [--registry registry/plan.json]
python3 -m careplan.cli return     --data data --case H1 --week 2026-05-25
python3 -m careplan.cli review     --data data --registry registry/plan.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import backtest_combo, rolling_cutoffs, summarize_coverage
from .capacity import FlexRegistry, ShiftCalendar
from .history import estimate_combos
from .planning import (
    ProgramRequest, approve_program, attribute_losses, early_return_replan,
)
from .registry import Registry
from .sources import load_bundle
from .timeline import build_ledger, extend_horizon, week_start
from .timeutils import add_weeks, parse_date


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_ledger(args) -> None:
    bundle = load_bundle(args.data)
    as_of = parse_date(args.as_of)
    station = args.station or bundle.accounts[0].station
    ledger = build_ledger(bundle.accounts, as_of, station)
    through = add_weeks(week_start(parse_date(args.through)), 0) if args.through else max(
        (r.week for r in bundle.shifts if r.station == station), default=as_of
    )
    extend_horizon(ledger, through)
    out = {"as_of": as_of.isoformat(), "station": station, "weeks": []}
    for wk in sorted({w for tl in ledger.cases.values() for w in tl.weeks}):
        recs = ledger.records_in(wk)
        out["weeks"].append({
            "week": wk.isoformat(),
            "occupied_minutes": ledger.occupied_minutes(wk),
            "held_minutes": ledger.held_minutes(wk),
            "releasable_minutes": ledger.releasable_minutes(wk),
            "states": {r.case: r.state.value for r in recs},
            "pending_renewal": [r.case for r in ledger.pending_renewals(wk)],
        })
    _print(out)


def cmd_history(args) -> None:
    bundle = load_bundle(args.data)
    station = args.station
    coverage = _coverage_map(bundle, station) if station else {}
    policy = _policy(bundle, station)
    estimates = estimate_combos(bundle.past, bundle.cutoffs, policy, coverage)
    _print({"estimates": [_est_dict(e) for e in estimates if not station or e.station == station]})


def cmd_backtest(args) -> None:
    bundle = load_bundle(args.data)
    result = []
    for cutoff in bundle.cutoffs:
        if args.station and cutoff.station != args.station:
            continue
        policy = next((p for p in bundle.policies if p.station == cutoff.station), None)
        horizon = policy.min_outcome_weeks if policy else 6
        combos = sorted({(e.frequency, e.minutes) for e in bundle.past if e.station == cutoff.station})
        first = min(e.effective for e in bundle.past if e.station == cutoff.station)
        # 滚动截点：从首批后 8 周起，每 4 周一个，止于观察止日前 horizon 周
        last = add_weeks(cutoff.observed_through, -horizon)
        cuts = rolling_cutoffs(add_weeks(week_start(first), 8), last, 4)
        for freq, minutes in combos:
            fs = backtest_combo(
                bundle.past, cutoff.station, freq, minutes, cuts,
                cutoff.observed_through, horizon,
            )
            for s in summarize_coverage(fs):
                result.append({
                    "station": s.station, "frequency": s.frequency, "minutes": s.minutes,
                    "checkable": s.checkable, "covered": s.covered,
                    "uncheckable": s.uncheckable,
                    "coverage": None if s.coverage is None else round(s.coverage, 4),
                })
    _print({"coverage": result})


def cmd_approve(args) -> None:
    bundle = load_bundle(args.data)
    as_of = parse_date(args.as_of)
    station = args.station or bundle.accounts[0].station
    policy = bundle.policy_for(station)
    ledger = build_ledger(bundle.accounts, as_of, station)
    cal = ShiftCalendar.from_rows(bundle.shifts, station, as_of)
    flex = FlexRegistry(bundle.flex)
    through = add_weeks(week_start(parse_date(args.frm)), args.weeks - 1)
    extend_horizon(ledger, through)

    # 先回验拿覆盖率，再做组合估计，保证 estimable 判定有依据
    coverage = _coverage_map(bundle, station)
    estimates = {
        (e.station, e.frequency, e.minutes): e
        for e in estimate_combos(bundle.past, bundle.cutoffs, policy, coverage)
    }
    prior = {}
    reg = Registry(Path(args.registry)) if args.registry else None
    if reg:
        prior = {parse_date(k): v for k, v in reg.approved_commitments().items()}

    req = ProgramRequest(
        ref=args.ref, station=station, start=week_start(parse_date(args.frm)),
        weeks=args.weeks, frequency=args.frequency, minutes=args.minutes,
    )
    decision = approve_program(
        req, as_of, ledger, cal, flex,
        [o for o in bundle.offers if o.station == station],
        estimates, policy.max_unfunded_risk, policy.max_cost_cents, prior,
    )
    payload = decision.to_dict()
    if reg:
        entry = reg.record_decision(payload)
        payload["registry_version"] = entry["version"]
    _print(payload)


def cmd_return(args) -> None:
    bundle = load_bundle(args.data)
    as_of = parse_date(args.as_of)
    station = args.station or bundle.accounts[0].station
    ledger = build_ledger(bundle.accounts, as_of, station)
    cal = ShiftCalendar.from_rows(bundle.shifts, station, as_of)
    flex = FlexRegistry(bundle.flex)
    result = early_return_replan(
        args.case, week_start(parse_date(args.week)), ledger, cal, flex, {}
    )
    _print(json.loads(json.dumps(result, ensure_ascii=False, default=str)))


def cmd_review(args) -> None:
    bundle = load_bundle(args.data)
    reg = Registry(Path(args.registry))
    # 以已批准需求的计划分钟为基准，实际安排从当前台账重放
    planned: dict = {}
    for wk, mins in reg.approved_commitments().items():
        planned[parse_date(wk)] = mins
    as_of = parse_date(args.as_of)
    station = bundle.accounts[0].station
    ledger = build_ledger(bundle.accounts, as_of, station)
    # 凭据原因 -> 三类资源损失口径
    reason_tag = {
        "accepted": "renewal_gap",        # 续约偏差导致的计划重排
        "pending": "renewal_gap",
        "return_right": "family_return",  # 家庭实际恢复/临时离家回归
        "relocated": "substitute_default",
        "nonrenewal": "renewal_gap",
    }
    actual = {}
    for wk in planned:
        recs = ledger.records_in(wk)
        reasons: dict[str, int] = {}
        for r in recs:
            tag = reason_tag.get(r.reason, "substitute_default")
            reasons[tag] = reasons.get(tag, 0) + r.weekly_minutes
        actual[wk] = {"minutes": ledger.occupied_minutes(wk), "reasons": reasons}
    rows = attribute_losses(planned, actual)
    total_loss = sum(row["loss_minutes"] for row in rows)
    summary = f"共 {len(rows)} 周，资源损失合计 {total_loss} 分钟"
    reg.record_review("latest", rows, summary)
    _print({"rows": rows, "summary": summary})


# -------- helpers --------

def _policy(bundle, station):
    if not station:
        return None
    return next((p for p in bundle.policies if p.station == station), None)


def _est_dict(e) -> dict:
    return {
        "station": e.station, "combo": f"{e.frequency}x{e.minutes}",
        "cohorts": list(e.cohorts), "starts": e.starts,
        "renew_accepted": e.renew_accepted, "nonrenewal": e.nonrenewal,
        "censored_renewals": e.censored_renewals, "relocated": e.relocated,
        "person_weeks": e.person_weeks,
        "renew_rate": _round(e.renew_rate),
        "renew_ci": [_round(e.renew_low), _round(e.renew_high)],
        "relocate_hazard": _round(e.relocate_hazard),
        "relocate_ci": [_round(e.relocate_low), _round(e.relocate_high)],
        "estimable": e.estimable, "reason": e.reason,
    }


def _round(v):
    return None if v is None else round(v, 4)


def _coverage_map(bundle, station: str | None) -> dict:
    coverage = {}
    if not station:
        return coverage
    for cutoff in bundle.cutoffs:
        if cutoff.station != station:
            continue
        policy = next((p for p in bundle.policies if p.station == station), None)
        horizon = policy.min_outcome_weeks if policy else 6
        combos = sorted({(e.frequency, e.minutes) for e in bundle.past if e.station == station})
        first = min(e.effective for e in bundle.past if e.station == station)
        last = add_weeks(week_start(cutoff.observed_through), -horizon)
        cuts = rolling_cutoffs(add_weeks(week_start(first), 8), last, 4)
        for freq, minutes in combos:
            fs = backtest_combo(bundle.past, station, freq, minutes, cuts,
                                cutoff.observed_through, horizon)
            for s in summarize_coverage(fs):
                if s.coverage is not None:
                    coverage[(station, freq, minutes)] = s.coverage
    return coverage


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="careplan", description="上门照护可承接计划工具")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--data", default="data")
        sp.add_argument("--station", default=None)
        sp.add_argument("--as-of", default="2026-05-15")

    sp = sub.add_parser("ledger"); add_common(sp)
    sp.add_argument("--through", default=None); sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("history"); add_common(sp); sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("backtest"); add_common(sp); sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("approve"); add_common(sp)
    sp.add_argument("--ref", required=True); sp.add_argument("--from", dest="frm", required=True)
    sp.add_argument("--weeks", type=int, required=True)
    sp.add_argument("--frequency", type=int, required=True)
    sp.add_argument("--minutes", type=int, required=True)
    sp.add_argument("--registry", default=None); sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("return"); add_common(sp)
    sp.add_argument("--case", required=True); sp.add_argument("--week", required=True)
    sp.set_defaults(func=cmd_return)

    sp = sub.add_parser("review")
    sp.add_argument("--data", default="data"); sp.add_argument("--as-of", default="2026-05-15")
    sp.add_argument("--registry", required=True); sp.set_defaults(func=cmd_review)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
