"""合成数据的读取、形状校验与类型化行对象。

只读标准库 CSV。空字符串保留为 None，绝不自动当零；
数字字段显式转换，日期字段交由 timeutils 解析。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .timeutils import parse_date


@dataclass(frozen=True)
class AccountEvent:
    event: str
    case: str
    station: str
    effective: object          # dt.date
    known_at: object
    kind: str                  # start/pause/resume/due_renewal/renew/close
    frequency: int
    minutes: int
    reserved_shift: str | None
    reason: str | None


@dataclass(frozen=True)
class ShiftRow:
    shift: str
    station: str
    week: object               # 周一
    available_minutes: int
    has_fixed: bool
    recorded_at: object


@dataclass(frozen=True)
class FlexAgreement:
    document: str
    case: str
    start: object
    weeks: int
    weekly_minutes: int
    borrowed_shift: str
    substitute: str | None
    signed_at: object


@dataclass(frozen=True)
class Offer:
    offer: str
    station: str
    start: object
    weeks: int
    weekly_minutes: int
    expense_cents: int
    expires_at: object


@dataclass(frozen=True)
class Policy:
    station: str
    min_cohorts: int
    min_outcome_weeks: int
    min_interval_coverage: float
    max_unfunded_risk: float
    max_cost_cents: int


@dataclass(frozen=True)
class PastEvent:
    event: str
    case: str
    station: str
    effective: object
    known_at: object
    kind: str
    frequency: int
    minutes: int
    reason: str | None


@dataclass(frozen=True)
class Cutoff:
    station: str
    observed_through: object
    recorded_at: object


@dataclass(frozen=True)
class DataBundle:
    accounts: list[AccountEvent]
    shifts: list[ShiftRow]
    flex: list[FlexAgreement]
    offers: list[Offer]
    policies: list[Policy]
    past: list[PastEvent]
    cutoffs: list[Cutoff]

    def policy_for(self, station: str) -> Policy:
        for p in self.policies:
            if p.station == station:
                return p
        raise KeyError(f"planning_policy.csv 缺少站点 {station} 的参数")


def _text(raw: str) -> str | None:
    raw = raw.strip()
    return raw or None


def _int(raw: str, field: str, row_no: int) -> int:
    raw = raw.strip()
    if not raw:
        raise ValueError(f"第 {row_no} 行字段 {field} 为空，不能当零")
    return int(raw)


def _bool_yes(raw: str) -> bool:
    raw = raw.strip().lower()
    if raw not in ("yes", "no"):
        raise ValueError(f"has_fixed 取值非法: {raw!r}")
    return raw == "yes"


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None:
            raise ValueError(f"{path.name} 缺少表头")
        rows = list(reader)
    return list(reader.fieldnames), rows


def load_bundle(data_dir: str | Path, contract_path: str | Path | None = None) -> DataBundle:
    data_dir = Path(data_dir)
    contract_path = Path(contract_path) if contract_path else data_dir.parent / "data-contract.json"

    if contract_path.exists():
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        expected = {name: spec["columns"] for name, spec in contract["files"].items()}
        for name, cols in expected.items():
            header, _ = _read(data_dir / name)
            if header != cols:
                raise ValueError(f"{name} 表头与 data-contract.json 不符: {header} != {cols}")

    _, acc_rows = _read(data_dir / "accounts.csv")
    accounts = [
        AccountEvent(
            event=r["event"], case=r["case"], station=r["station"],
            effective=parse_date(r["effective"]), known_at=parse_date(r["known_at"]),
            kind=r["kind"], frequency=_int(r["frequency"], "frequency", i),
            minutes=_int(r["minutes"], "minutes", i),
            reserved_shift=_text(r["reserved_shift"]), reason=_text(r["reason"]),
        )
        for i, r in enumerate(acc_rows, start=2)
    ]

    _, sh_rows = _read(data_dir / "shift_calendar.csv")
    shifts = [
        ShiftRow(
            shift=r["shift"], station=r["station"], week=parse_date(r["week"]),
            available_minutes=_int(r["available_minutes"], "available_minutes", i),
            has_fixed=_bool_yes(r["has_fixed"]), recorded_at=parse_date(r["recorded_at"]),
        )
        for i, r in enumerate(sh_rows, start=2)
    ]

    _, fl_rows = _read(data_dir / "flex_agreements.csv")
    flex = [
        FlexAgreement(
            document=r["document"], case=r["case"], start=parse_date(r["from"]),
            weeks=_int(r["weeks"], "weeks", i),
            weekly_minutes=_int(r["weekly_minutes"], "weekly_minutes", i),
            borrowed_shift=r["borrowed_shift"], substitute=_text(r["substitute"]),
            signed_at=parse_date(r["signed_at"]),
        )
        for i, r in enumerate(fl_rows, start=2)
    ]

    _, of_rows = _read(data_dir / "offers.csv")
    offers = [
        Offer(
            offer=r["offer"], station=r["station"], start=parse_date(r["from"]),
            weeks=_int(r["weeks"], "weeks", i),
            weekly_minutes=_int(r["weekly_minutes"], "weekly_minutes", i),
            expense_cents=_int(r["expense_cents"], "expense_cents", i),
            expires_at=parse_date(r["expires_at"]),
        )
        for i, r in enumerate(of_rows, start=2)
    ]

    _, po_rows = _read(data_dir / "planning_policy.csv")
    policies = [
        Policy(
            station=r["station"], min_cohorts=_int(r["min_cohorts"], "min_cohorts", i),
            min_outcome_weeks=_int(r["min_outcome_weeks"], "min_outcome_weeks", i),
            min_interval_coverage=float(r["min_interval_coverage"]),
            max_unfunded_risk=float(r["max_unfunded_risk"]),
            max_cost_cents=_int(r["max_cost_cents"], "max_cost_cents", i),
        )
        for i, r in enumerate(po_rows, start=2)
    ]

    _, pa_rows = _read(data_dir / "past_events.csv")
    past = [
        PastEvent(
            event=r["event"], case=r["case"], station=r["station"],
            effective=parse_date(r["effective"]), known_at=parse_date(r["known_at"]),
            kind=r["kind"], frequency=_int(r["frequency"], "frequency", i),
            minutes=_int(r["minutes"], "minutes", i), reason=_text(r["reason"]),
        )
        for i, r in enumerate(pa_rows, start=2)
    ]

    _, co_rows = _read(data_dir / "history_cutoffs.csv")
    cutoffs = [
        Cutoff(
            station=r["station"], observed_through=parse_date(r["observed_through"]),
            recorded_at=parse_date(r["recorded_at"]),
        )
        for i, r in enumerate(co_rows, start=2)
    ]

    _validate_integrity(accounts, shifts, flex, offers, past)
    return DataBundle(accounts, shifts, flex, offers, policies, past, cutoffs)


def _validate_integrity(accounts, shifts, flex, offers, past) -> None:
    # 凭据编号全局不重复（同一文件内）
    for rows, label in ((accounts, "accounts"), (past, "past_events")):
        ids = [r.event for r in rows]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{label} 存在重复 event 编号")
    for r in accounts:
        if r.effective.weekday() != 0:
            raise ValueError(f"accounts 凭据 {r.event} 生效日不是周一")
    for r in shifts:
        if r.week.weekday() != 0:
            raise ValueError(f"班次 {r.shift} 的周锚点不是周一")
    for r in flex:
        if r.start.weekday() != 0:
            raise ValueError(f"复用约 {r.document} 起始日不是周一")
    for r in offers:
        if r.start.weekday() != 0:
            raise ValueError(f"采购 {r.offer} 起始日不是周一")
    # 同站点班次每周分钟不得为负
    if any(r.available_minutes < 0 for r in shifts):
        raise ValueError("available_minutes 出现负数")
