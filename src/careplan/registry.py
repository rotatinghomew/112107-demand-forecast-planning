"""审批决定、复用/替代登记与回看记录的持久化登记簿。

使用 JSON 文件（标准库即可），每条记录带决策时间与依据，
支持同一项目反复审订：同一 request_ref 的新决定不覆盖旧决定，而是追加版本，
便于从以前的审批追查当时风险与每周可承诺余额。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from .timeutils import add_weeks, parse_date


@dataclass
class Registry:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"decisions": [], "reuse_records": [], "reviews": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- 审批决定 -------------------------------------------------------
    def record_decision(self, decision_dict: dict) -> dict:
        payload = self._read()
        ref = decision_dict["request_ref"]
        version = sum(
            1 for d in payload["decisions"]
            if d["decision"]["request_ref"] == ref
        ) + 1
        entry = {
            "version": version,
            "recorded_at": dt.date.today().isoformat(),
            "decision": decision_dict,
        }
        payload["decisions"].append(entry)
        self._write(payload)
        return entry

    def decisions_for(self, request_ref: str) -> list[dict]:
        return [
            d for d in self._read()["decisions"]
            if d["decision"]["request_ref"] == request_ref
        ]

    def approved_commitments(self) -> dict[str, int]:
        """已批准且未撤销的跨周占用：{ISO 周一: 分钟}，供下一次计划扣减。"""
        commitments: dict[dt.date, int] = {}
        for entry in self._read()["decisions"]:
            d = entry["decision"]
            if not d["approved"]:
                continue
            start = parse_date(d["week_decisions"][0]["week"])
            weekly = d["week_decisions"][0]["need"]
            for i, wd in enumerate(d["week_decisions"]):
                if wd["feasible"]:
                    wk = add_weeks(start, i)
                    commitments[wk] = commitments.get(wk, 0) + weekly
        return {wk.isoformat(): mins for wk, mins in sorted(commitments.items())}

    # ---- 复用/替代登记 --------------------------------------------------
    def record_reuse(self, document: str, case: str, borrowed_shift: str,
                     substitute: str | None, weeks: list[str], note: str) -> None:
        payload = self._read()
        payload["reuse_records"].append({
            "document": document, "case": case,
            "borrowed_shift": borrowed_shift, "substitute": substitute,
            "weeks": weeks, "note": note,
            "recorded_at": dt.date.today().isoformat(),
        })
        self._write(payload)

    def reuse_records(self) -> list[dict]:
        return self._read()["reuse_records"]

    # ---- 回看兑现 -------------------------------------------------------
    def record_review(self, request_ref: str, rows: list[dict], summary: str) -> None:
        payload = self._read()
        payload["reviews"].append({
            "request_ref": request_ref, "rows": rows, "summary": summary,
            "recorded_at": dt.date.today().isoformat(),
        })
        self._write(payload)

    def reviews(self, request_ref: str | None = None) -> list[dict]:
        data = self._read()["reviews"]
        if request_ref is None:
            return data
        return [r for r in data if r["request_ref"] == request_ref]
