"""周一锚定的周历工具。

业务周以周一开始。所有凭据按其生效日所在的周一归入业务周；
周中（非周一）生效的事件影响该周的占用，释放在下一周生效。
"""

from __future__ import annotations

import datetime as dt

WEEK = dt.timedelta(weeks=1)


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value.strip())


def week_start(value: dt.date | str) -> dt.date:
    """返回该日期所在业务周的周一。"""
    day = parse_date(value) if isinstance(value, str) else value
    return day - dt.timedelta(days=day.weekday())


def add_weeks(monday: dt.date, n: int) -> dt.date:
    return monday + WEEK * n


def weeks_between(start_monday: dt.date, end_monday: dt.date) -> int:
    """两个周一之间的整周数（end - start），允许为负。"""
    delta = end_monday - start_monday
    if delta.days % 7:
        raise ValueError(f"两端不是同一星期相位: {start_monday} .. {end_monday}")
    return delta.days // 7


def iter_weeks(start_monday: dt.date, count: int):
    for i in range(count):
        yield add_weeks(start_monday, i)


def week_label(monday: dt.date) -> str:
    return monday.isoformat()


def inclusive_week_range(first: dt.date, last: dt.date):
    """生成从 first 周一到 last 周一（含）的所有周一。"""
    cur = week_start(first)
    end = week_start(last)
    while cur <= end:
        yield cur
        cur = add_weeks(cur, 1)
