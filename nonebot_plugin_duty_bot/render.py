"""图片渲染与纯文本降级。

- 多条目列表场景（我的排班/个人每周预告/群今日/明日/每周名单）以图片输出。
- 渲染使用 nonebot-plugin-htmlkit（litehtml），不加载任何外部网络资源。
- 姓名、岗位等数据经 jinja2 autoescape 转义后再拼入模板。
- 渲染或发送异常时降级为纯文本分段发送，不向用户暴露内部错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape
from nonebot import logger, require

require("nonebot_plugin_htmlkit")
from nonebot_plugin_htmlkit import html_to_pic  # noqa: E402

from .api import Assignment, Roster
from .sender import CAT_COMMAND, SendResult, get_queue
from .timeutil import format_date_cn, weekday_name

_ROLE_CN = {"primary": "主值", "backup": "备值"}
_MAX_CHUNK_CHARS = 900

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(("html", "j2")),
    trim_blocks=True,
    lstrip_blocks=True,
)


async def _deny_fetch(url: str) -> None:
    """禁用一切外部资源加载（图片/CSS 均只允许内联）。"""
    return None


# ---------- 视图模型 ----------


@dataclass
class PersonalRow:
    cycle_name: str
    week_no: int
    weekday: str
    date_display: str
    time_range: str
    post_name: str
    role: str


@dataclass
class PersonalView:
    title: str
    subtitle: str
    rows: list[PersonalRow] = field(default_factory=list)


@dataclass
class SlotRow:
    post_name: str
    time_range: str
    primary: str
    backup: str


@dataclass
class DaySection:
    date_display: str
    rows: list[SlotRow] = field(default_factory=list)


@dataclass
class GroupDayView:
    title: str
    date_display: str
    rows: list[SlotRow] = field(default_factory=list)


@dataclass
class GroupWeekView:
    title: str
    days: list[DaySection] = field(default_factory=list)


# ---------- 视图构建 ----------


def _role_cn(role: str) -> str:
    return _ROLE_CN.get(role, role)


def _assignment_sort_key(a: Assignment) -> tuple:
    """有日期班次按日期+开始时间在前，历史班次（无日期）在后。"""
    if a.date:
        return (0, a.date, a.start_time, a.cycle_name, a.week_no, a.weekday)
    return (1, a.cycle_name, a.week_no, a.weekday, a.start_time)


def build_personal_view(
    title: str, subtitle: str, assignments: Iterable[Assignment]
) -> PersonalView:
    view = PersonalView(title=title, subtitle=subtitle)
    for a in sorted(assignments, key=_assignment_sort_key):
        date_display = (
            format_date_cn(date.fromisoformat(a.date))
            if a.date
            else "日期未定（历史班次）"
        )
        view.rows.append(
            PersonalRow(
                cycle_name=a.cycle_name,
                week_no=a.week_no,
                weekday=weekday_name(a.weekday),
                date_display=date_display,
                time_range=f"{a.start_time}-{a.end_time}",
                post_name=a.post_name,
                role=_role_cn(a.role),
            )
        )
    return view


def _aggregate_slots(assignments: Sequence[Assignment]) -> list[SlotRow]:
    """按班次聚合：主/备合并同行，多个主值或备值以顿号连接，缺省显示“—”。"""
    grouped: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    for a in assignments:
        if a.slot_id not in grouped:
            grouped[a.slot_id] = {"primary": [], "backup": []}
            order.append(a.slot_id)
        if a.role in grouped[a.slot_id]:
            if a.name not in grouped[a.slot_id][a.role]:
                grouped[a.slot_id][a.role].append(a.name)
    rows: list[tuple[SlotRow, str]] = []
    for slot_id in order:
        first = next(a for a in assignments if a.slot_id == slot_id)
        names = grouped[slot_id]
        rows.append(
            (
                SlotRow(
                    post_name=first.post_name,
                    time_range=f"{first.start_time}-{first.end_time}",
                    primary="、".join(names["primary"]) or "—",
                    backup="、".join(names["backup"]) or "—",
                ),
                first.start_time,
            )
        )
    rows.sort(key=lambda pair: pair[1])
    return [row for row, _ in rows]


def build_group_day_view(
    title: str, date_display: str, assignments: Sequence[Assignment]
) -> GroupDayView:
    return GroupDayView(
        title=title, date_display=date_display, rows=_aggregate_slots(assignments)
    )


def build_group_week_view(
    title: str, assignments: Sequence[Assignment]
) -> GroupWeekView:
    by_date: dict[str, list[Assignment]] = {}
    for a in assignments:
        if a.date:
            by_date.setdefault(a.date, []).append(a)
    view = GroupWeekView(title=title)
    for iso_date in sorted(by_date):
        rows = _aggregate_slots(by_date[iso_date])
        if rows:
            view.days.append(
                DaySection(date_display=format_date_cn(date.fromisoformat(iso_date)), rows=rows)
            )
    return view


# ---------- 排班筛选 ----------


def filter_by_dates(
    roster: Roster,
    start: date,
    end: date,
    student_no: Optional[str] = None,
) -> list[Assignment]:
    """筛选日期落在 [start, end]（含）的班次；可按学号过滤。日期未定班次不参与。"""
    return [
        a
        for a in roster.assignments
        if a.date
        and start <= date.fromisoformat(a.date) <= end
        and (student_no is None or a.student_no == student_no)
    ]


# ---------- 渲染 ----------


async def render_template(template_name: str, view) -> bytes:
    """渲染视图为 PNG 图片字节。渲染失败抛异常，由调用方降级。"""
    html = _env.get_template(template_name).render(view=view)
    return await html_to_pic(
        html,
        img_fetch_fn=_deny_fetch,
        css_fetch_fn=_deny_fetch,
    )


# ---------- 纯文本降级 ----------


def _chunk_lines(lines: list[str], max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if current and length + len(line) > max_chars:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def personal_text_chunks(view: PersonalView) -> list[str]:
    lines = [f"【{view.title}】", view.subtitle] if view.subtitle else [f"【{view.title}】"]
    for i, row in enumerate(view.rows, 1):
        lines.append(
            f"{i}. {row.date_display} {row.time_range} | {row.post_name} | {row.role} "
            f"| {row.cycle_name} 第{row.week_no}周·{row.weekday}"
        )
    if not view.rows:
        lines.append("暂无排班")
    return _chunk_lines(lines)


def _slot_row_text(row: SlotRow) -> str:
    return f"· {row.time_range} | {row.post_name} | 主值: {row.primary} | 备值: {row.backup}"


def group_day_text_chunks(view: GroupDayView) -> list[str]:
    lines = [f"【{view.title}】{view.date_display}"]
    lines.extend(_slot_row_text(row) for row in view.rows)
    if not view.rows:
        lines.append("无值班安排")
    return _chunk_lines(lines)


def group_week_text_chunks(view: GroupWeekView) -> list[str]:
    lines = [f"【{view.title}】"]
    for section in view.days:
        lines.append(f"—— {section.date_display} ——")
        lines.extend(_slot_row_text(row) for row in section.rows)
    if not view.days:
        lines.append("无值班安排")
    return _chunk_lines(lines)


# ---------- 发送（图片优先、降级纯文本） ----------


async def deliver_view(
    bot,
    chat_type: str,
    target_id: int,
    *,
    template_name: str,
    view,
    text_chunks,
    category: str = CAT_COMMAND,
) -> SendResult:
    """渲染图片发送；生成或发送异常时回退纯文本分段发送。

    返回聚合发送结果：仅当全部消息实际发送成功才视为成功。
    """
    from nonebot.adapters.onebot.v11 import MessageSegment

    queue = get_queue()
    try:
        image = await render_template(template_name, view)
        result = await queue.enqueue(
            bot, chat_type, target_id, MessageSegment.image(image), category
        )
        if result.ok:
            return SendResult(True)
        logger.warning(
            f"图片发送失败，降级纯文本[{category}] {chat_type}({target_id}): {result.error}"
        )
    except Exception as e:
        logger.warning(
            f"图片渲染异常，降级纯文本[{category}] {chat_type}({target_id}): "
            f"{e.__class__.__name__}: {e}"
        )

    ok = True
    error: str | None = None
    for chunk in text_chunks(view):
        result = await queue.enqueue(bot, chat_type, target_id, chunk, category)
        if not result.ok:
            ok = False
            error = result.error
    return SendResult(ok, error)
