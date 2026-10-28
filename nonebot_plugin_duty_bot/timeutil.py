"""北京时间与周期（周界）工具。

所有调度与日期判断均按北京时间（Asia/Shanghai）执行。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def now_beijing() -> datetime:
    """当前北京时间。"""
    return datetime.now(BEIJING)


def weekday_name(weekday: int) -> str:
    """将 OneBot 语义的 weekday（1=周一 … 7=周日）转为中文名。"""
    if 1 <= weekday <= 7:
        return WEEKDAY_NAMES[weekday - 1]
    return f"星期{weekday}"


def week_monday(d: date) -> date:
    """d 所在周的周一。"""
    return d - timedelta(days=d.weekday())


def this_week_range(d: date) -> tuple[date, date]:
    """d 所在周（周一 ~ 周日）。"""
    monday = week_monday(d)
    return monday, monday + timedelta(days=6)


def next_week_range(d: date) -> tuple[date, date]:
    """d 下一周（周一 ~ 周日）。"""
    monday = week_monday(d) + timedelta(days=7)
    return monday, monday + timedelta(days=6)


def assignment_start(date_: date, start_time: str) -> datetime:
    """班次开始时刻（北京时间）。start_time 形如 'HH:MM'。"""
    hour, minute = start_time.split(":")
    return datetime.combine(date_, time(int(hour), int(minute)), tzinfo=BEIJING)


def format_date_cn(d: date) -> str:
    """中文日期，如「10月27日（周一）」。"""
    return f"{d.month}月{d.day}日（{WEEKDAY_NAMES[d.weekday()]}）"


def format_uptime(seconds: float) -> str:
    """进程运行时长，如「2天03:12:55」。"""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}天{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
