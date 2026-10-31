"""图片渲染与纯文本降级。

- 多条目列表场景（我的排班/个人每周预告/群今日/明日/每周名单）以图片输出。
- 渲染使用 nonebot-plugin-htmlkit（litehtml），不加载任何外部网络资源。
- 姓名、岗位等数据经 jinja2 autoescape 转义后再拼入模板。
- 渲染异常或发送明确失败（协议端拒绝）时降级为纯文本分段发送，不向用户暴露内部错误。
- 发送结果未知（超时/网络异常，图片可能已实际送达）时不降级，避免图片与文字重复。
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
from .config import plugin_config
from .sender import CAT_COMMAND, SendResult, get_queue
from .timeutil import WEEKDAY_FULL_NAMES, format_date_cn

_MAX_CHUNK_CHARS = 900
# 缩放基准：scale=1 时根字号（px）与渲染画布最大宽度（px）
_SCALE_BASE_ROOT_FONT = 16.0
_SCALE_BASE_MAX_WIDTH = 800.0
# 周网格含 8 列，基准画布单独加宽；人名不折行，列宽须容纳多人名
_TEMPLATE_MAX_WIDTH = {"group_week.html.j2": 1120.0}

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
    week_no: int
    date_display: str
    time_range: str
    post_name: str


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


@dataclass
class WeekDayHeader:
    weekday: str
    date_display: str


@dataclass
class GroupDayView:
    title: str
    date_display: str
    rows: list[SlotRow] = field(default_factory=list)


@dataclass
class GroupWeekView:
    title: str
    date_range: str
    headers: list[WeekDayHeader]
    slot_times: list[str]
    cells: list[list[list[SlotRow]]]
    # 画布基准宽度（px，scale=1）：按日期列数自适应，0 表示用模板缺省基准
    canvas_width: int = 0


# ---------- 视图构建 ----------


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
                week_no=a.week_no,
                date_display=date_display,
                time_range=f"{a.start_time}-{a.end_time}",
                post_name=a.post_name,
            )
        )
    return view


def _aggregate_slots(assignments: Sequence[Assignment]) -> list[SlotRow]:
    """按班次聚合：同班次多个值班人以顿号连接。"""
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for a in assignments:
        if a.slot_id not in grouped:
            grouped[a.slot_id] = []
            order.append(a.slot_id)
        if a.name not in grouped[a.slot_id]:
            grouped[a.slot_id].append(a.name)
    rows: list[tuple[SlotRow, str]] = []
    for slot_id in order:
        first = next(a for a in assignments if a.slot_id == slot_id)
        rows.append(
            (
                SlotRow(
                    post_name=first.post_name,
                    time_range=f"{first.start_time}-{first.end_time}",
                    primary="、".join(grouped[slot_id]) or "—",
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
    title: str, monday: date, assignments: Sequence[Assignment]
) -> GroupWeekView:
    """构建周网格视图：仅保留有排班的日期列（整天无排班不渲染该列），
    行=去重升序时段，格子=当日班次聚合。"""
    week_dates = [monday + timedelta(days=i) for i in range(7)]
    active_days = [
        (d, rows)
        for d in week_dates
        if (rows := _aggregate_slots(
            [a for a in assignments if a.date == d.isoformat()]
        ))
    ]
    headers = [
        WeekDayHeader(
            weekday=WEEKDAY_FULL_NAMES[(d - monday).days],
            date_display=f"{d.month:02d}-{d.day:02d}",
        )
        for d, _ in active_days
    ]
    starts: dict[str, str] = {}
    for _, rows in active_days:
        for r in rows:
            starts.setdefault(r.time_range, r.time_range.split("-")[0])
    slot_times = sorted(starts, key=starts.get)
    index = {t: i for i, t in enumerate(slot_times)}
    cells: list[list[list[SlotRow]]] = [[[] for _ in active_days] for _ in slot_times]
    for day_idx, (_, rows) in enumerate(active_days):
        for r in rows:
            cells[index[r.time_range]][day_idx].append(r)
    if active_days:
        first, last = active_days[0][0], active_days[-1][0]
        date_range = (
            f"{first.month}月{first.day}日 - {last.month}月{last.day}日"
            if first != last
            else f"{first.month}月{first.day}日"
        )
    else:
        sunday = week_dates[-1]
        date_range = f"{monday.month}月{monday.day}日 - {sunday.month}月{sunday.day}日"
    return GroupWeekView(
        title=title,
        date_range=date_range,
        headers=headers,
        slot_times=slot_times,
        cells=cells,
        # 每列基准 140px（时段列+日期列），下限 800；全周 8 列即 1120
        canvas_width=max(800, (len(headers) + 1) * 140),
    )


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
    """渲染视图为 PNG 图片字节。渲染失败抛异常，由调用方降级。

    模板尺寸均以 rem 定义，根字号随 dutybot_render_scale 等比放大，
    画布最大宽度（按模板基准，周网格随日期列数自适应）同步放大，
    从而输出更高分辨率的图片。
    """
    scale = plugin_config.dutybot_render_scale
    base_width = _TEMPLATE_MAX_WIDTH.get(template_name, _SCALE_BASE_MAX_WIDTH)
    if canvas := getattr(view, "canvas_width", 0):
        base_width = float(canvas)
    html = _env.get_template(template_name).render(
        view=view, root_font_size=_SCALE_BASE_ROOT_FONT * scale
    )
    return await html_to_pic(
        html,
        max_width=base_width * scale,
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
            f"{i}. {row.date_display} · 第{row.week_no}周 {row.time_range} | {row.post_name}"
        )
    if not view.rows:
        lines.append("暂无排班")
    return _chunk_lines(lines)


def _slot_row_text(row: SlotRow) -> str:
    return f"· {row.time_range} | {row.post_name} | 值班人: {row.primary}"


def _week_row_text(row: SlotRow) -> str:
    return f"· {row.time_range} | {row.post_name} | 值班人: {row.primary}"


def group_day_text_chunks(view: GroupDayView) -> list[str]:
    lines = [f"【{view.title}】{view.date_display}"]
    lines.extend(_slot_row_text(row) for row in view.rows)
    if not view.rows:
        lines.append("无值班安排")
    return _chunk_lines(lines)


def group_week_text_chunks(view: GroupWeekView) -> list[str]:
    lines = [f"【{view.title}】{view.date_range}"]
    has_any = False
    for day_idx, header in enumerate(view.headers):
        rows = [r for col in view.cells for r in col[day_idx]]
        if not rows:
            continue
        has_any = True
        lines.append(f"—— {header.weekday} {header.date_display} ——")
        lines.extend(_week_row_text(r) for r in rows)
    if not has_any:
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
    """渲染图片发送；渲染异常或发送明确失败时回退纯文本分段发送。

    发送「结果未知」（超时/网络异常，图片可能已实际送达）时不回退纯文本，
    避免用户同时收到图片与文字两份重复内容。
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
        if result.uncertain:
            logger.warning(
                f"图片发送结果未知，跳过纯文本降级[{category}] {chat_type}({target_id}): "
                f"{result.error}"
            )
            return SendResult(False, f"图片发送结果未知: {result.error}", uncertain=True)
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
    uncertain = False
    for chunk in text_chunks(view):
        result = await queue.enqueue(bot, chat_type, target_id, chunk, category)
        if not result.ok:
            ok = False
            error = result.error
            uncertain = uncertain or result.uncertain
    return SendResult(ok, error, uncertain)
