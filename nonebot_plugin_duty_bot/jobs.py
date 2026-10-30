"""定时任务与主动触达。

- 所有任务经 nonebot-plugin-apscheduler 注册，按北京时间调度；
  同一任务不并发（max_instances=1）；停机期间错过的任务不自动补跑，
  运行期短暂延迟在容忍窗口（misfire_grace_time）内仍执行，超窗跳过。
- 幂等：群播报与每周预告以本期业务键幂等；值班前提醒逐班次内存去重。
- 定时任务在消息流程之外执行，自行处理无连接/发送失败/接口异常，
  记录执行结果后正常结束，不因异常中断调度。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from nonebot import get_bot, logger, require
from nonebot.adapters.onebot.v11 import Bot

from .api import DutyApiError, get_client
from .config import plugin_config
from .render import (
    build_group_day_view,
    build_group_week_view,
    build_personal_view,
    deliver_view,
    filter_by_dates,
    group_day_text_chunks,
    group_week_text_chunks,
    personal_text_chunks,
)
from .sender import (
    CAT_BROADCAST,
    CAT_FORECAST,
    CAT_REMIND,
    CHAT_GROUP,
    CHAT_PRIVATE,
    SendResult,
    get_queue,
)
from .storage import (
    TASK_GROUP_WEEKLY,
    TASK_PERSONAL_WEEKLY,
    TASK_REMIND_POLL,
    TASK_TOMORROW,
    get_db,
)
from .timeutil import (
    BEIJING,
    assignment_start,
    format_date_cn,
    next_week_range,
    now_beijing,
)

require("nonebot_plugin_apscheduler")

# 调度任务 ID（供「状态」查询下次执行时间）
JOB_REMIND_POLL = "dutybot_remind_poll"
JOB_PERSONAL_WEEKLY = "dutybot_personal_weekly"
JOB_GROUP_WEEKLY = "dutybot_group_weekly"
JOB_TOMORROW = "dutybot_tomorrow"

JOB_DISPLAY = {
    JOB_REMIND_POLL: "值班前提醒轮询",
    JOB_PERSONAL_WEEKLY: "个人每周排班预告",
    JOB_GROUP_WEEKLY: "群每周排班预告",
    JOB_TOMORROW: "明日值班播报",
}

# 调度任务 ID -> 任务状态 task_id 映射
JOB_TASK_MAP = {
    JOB_REMIND_POLL: TASK_REMIND_POLL,
    JOB_PERSONAL_WEEKLY: TASK_PERSONAL_WEEKLY,
    JOB_GROUP_WEEKLY: TASK_GROUP_WEEKLY,
    JOB_TOMORROW: TASK_TOMORROW,
}

# 值班提醒逐班次去重（内存态；重启后窗口仍开时允许重复收到一次，优先保证不漏）
_reminded: set[str] = set()

_MISFIRE_GRACE = 300  # 容忍窗口：5 分钟内延迟仍执行，超窗跳过


def try_get_bot() -> Bot | None:
    """获取当前可用的 Bot，无连接时返回 None。

    get_bot() 不传 self_id 且无 Bot 时抛 ValueError（NoneBot 2.x 行为）；
    传 self_id 时才抛 KeyError，一并覆盖以防降级。
    """
    try:
        return get_bot()
    except (KeyError, ValueError):
        return None


# ---------- 群播报核心（供定时任务与「重播」共用） ----------


async def broadcast_group_day(
    bot: Bot, day: date, *, title: str, announce_empty: bool = True
) -> SendResult:
    """按指定日期向配置群播报班次聚合名单（图片，降级纯文本）。

    announce_empty=False（定时播报）时无排班不发送任何消息，直接视为成功；
    默认 True（「重播」）时无排班仍发送文本提示，便于管理员确认结果。
    """
    if not plugin_config.dutybot_group_id:
        return SendResult(False, "未配置值班群号")
    roster = await get_client().get_roster()
    assignments = filter_by_dates(roster, day, day)
    if not assignments:
        if not announce_empty:
            logger.info(f"{title}：{format_date_cn(day)} 无值班安排，跳过播报")
            return SendResult(True)
        return await get_queue().enqueue(
            bot,
            CHAT_GROUP,
            plugin_config.dutybot_group_id,
            f"{title}：{format_date_cn(day)} 无值班安排",
            CAT_BROADCAST,
        )
    view = build_group_day_view(title, format_date_cn(day), assignments)
    return await deliver_view(
        bot,
        CHAT_GROUP,
        plugin_config.dutybot_group_id,
        template_name="group_day.html.j2",
        view=view,
        text_chunks=group_day_text_chunks,
        category=CAT_BROADCAST,
    )


async def broadcast_group_week(bot: Bot, monday: date, *, title: str) -> SendResult:
    """按指定周（周一起 7 天）向配置群播报周网格看板（图片，降级纯文本）。"""
    if not plugin_config.dutybot_group_id:
        return SendResult(False, "未配置值班群号")
    roster = await get_client().get_roster()
    assignments = filter_by_dates(roster, monday, monday + timedelta(days=6))
    if not assignments:
        return await get_queue().enqueue(
            bot,
            CHAT_GROUP,
            plugin_config.dutybot_group_id,
            f"{title}：无值班安排",
            CAT_BROADCAST,
        )
    view = build_group_week_view(title, monday, assignments)
    return await deliver_view(
        bot,
        CHAT_GROUP,
        plugin_config.dutybot_group_id,
        template_name="group_week.html.j2",
        view=view,
        text_chunks=group_week_text_chunks,
        category=CAT_BROADCAST,
    )


# ---------- 各定时任务 ----------


def _group_ready() -> bool:
    if not plugin_config.dutybot_group_id:
        logger.debug("未配置值班群号，跳过群播报任务")
        return False
    return True


async def _job_remind_poll() -> None:
    """值班前提醒轮询：窗口内开始、日期已定、尚未提醒的班次私聊提醒。"""
    db = get_db()
    now = now_beijing()
    if not get_client().configured:
        await db.set_task_state(TASK_REMIND_POLL, "failed", now.isoformat(), "接口未配置")
        return
    bot = try_get_bot()
    if bot is None:
        await db.set_task_state(TASK_REMIND_POLL, "failed", now.isoformat(), "无可用机器人连接")
        return
    try:
        roster = await get_client().get_roster()
    except DutyApiError as e:
        logger.warning(f"提醒轮询接口异常: {e.message}")
        await db.set_task_state(TASK_REMIND_POLL, "failed", now.isoformat(), e.message)
        return

    window = timedelta(minutes=plugin_config.dutybot_remind_window)
    bindings = {b.student_no: b for b in await db.all_bindings()}
    candidates: list = []
    unreachable = 0
    for a in roster.assignments:
        if not a.date or a.id in _reminded:
            continue
        start = assignment_start(date.fromisoformat(a.date), a.start_time)
        if now <= start <= now + window:
            candidates.append((a, start))
            if a.student_no not in bindings:
                unreachable += 1

    queue = get_queue()
    sent = failed = 0
    for a, start in candidates:
        binding = bindings.get(a.student_no)
        if binding is None:
            continue
        text = (
            f"【值班提醒】你有一班值班即将开始\n"
            f"{format_date_cn(date.fromisoformat(a.date))} {a.start_time}-{a.end_time}\n"
            f"岗位：{a.post_name}\n"
            f"请提前做好准备，准时到岗。"
        )
        _reminded.add(a.id)  # 先标记防止轮询重入；发送失败回滚以便下轮重试
        result = await queue.enqueue(bot, CHAT_PRIVATE, binding.qq, text, CAT_REMIND)
        if result.ok:
            sent += 1
        else:
            _reminded.discard(a.id)
            failed += 1

    summary = (
        f"本轮提醒：命中窗口 {len(candidates)} 班，已发送 {sent}，"
        f"发送失败 {failed}，无绑定 {unreachable}"
    )
    logger.info(f"提醒轮询摘要: {summary}")
    error = f"发送失败 {failed} 条" if failed else None
    await db.set_task_state(TASK_REMIND_POLL, "failed" if failed else "success", now.isoformat(), error)


async def _job_personal_weekly() -> None:
    """个人每周排班预告：对每个绑定用户私聊推送下周排班（图片）。"""
    db = get_db()
    now = now_beijing()
    monday, sunday = next_week_range(now.date())
    key = monday.isoformat()
    state = await db.get_task_state(TASK_PERSONAL_WEEKLY)
    if state and state.status == "success" and state.business_key == key:
        logger.debug(f"个人每周预告本期（{key}）已成功，跳过")
        return
    if not get_client().configured:
        await db.set_task_state(TASK_PERSONAL_WEEKLY, "failed", key, "接口未配置")
        return
    bot = try_get_bot()
    if bot is None:
        await db.set_task_state(TASK_PERSONAL_WEEKLY, "failed", key, "无可用机器人连接")
        return
    try:
        roster = await get_client().get_roster()
    except DutyApiError as e:
        await db.set_task_state(TASK_PERSONAL_WEEKLY, "failed", key, e.message)
        return

    bindings = await db.all_bindings()
    tasks: list[tuple] = []
    for binding in bindings:
        assignments = filter_by_dates(roster, monday, sunday, binding.student_no)
        if not assignments:
            continue
        view = build_personal_view(
            "下周排班预告", f"{binding.name}（{binding.student_no}）", assignments
        )
        tasks.append(
            (
                binding,
                deliver_view(
                    bot,
                    CHAT_PRIVATE,
                    binding.qq,
                    template_name="personal.html.j2",
                    view=view,
                    text_chunks=personal_text_chunks,
                    category=CAT_FORECAST,
                ),
            )
        )
    if not tasks:
        logger.info(f"个人每周预告：无用户有下周班次（本期 {key}）")
        await db.set_task_state(TASK_PERSONAL_WEEKLY, "success", key, None)
        return
    results = await asyncio.gather(*(task for _, task in tasks))
    failures = [b for (b, _), r in zip(tasks, results) if not r.ok]
    # 部分用户失败仅记录日志，不提供补发（决策 #3）
    if failures:
        names = "、".join(f"{b.qq}({b.name})" for b in failures[:5])
        error = f"{len(failures)} 人发送失败: {names}"
        logger.warning(f"个人每周预告部分发送失败（本期 {key}）: {error}")
        await db.set_task_state(TASK_PERSONAL_WEEKLY, "failed", key, error)
        return
    logger.info(f"个人每周预告本期（{key}）已发送 {len(results)} 人")
    await db.set_task_state(TASK_PERSONAL_WEEKLY, "success", key, None)


async def _job_group_weekly() -> None:
    """群每周排班预告：下周（周一~周日）名单按周网格看板播报（图片）。"""
    db = get_db()
    now = now_beijing()
    monday, _ = next_week_range(now.date())
    key = monday.isoformat()
    state = await db.get_task_state(TASK_GROUP_WEEKLY)
    if state and state.status == "success" and state.business_key == key:
        logger.debug(f"群每周预告本期（{key}）已成功，跳过")
        return
    if not _group_ready():
        await db.set_task_state(TASK_GROUP_WEEKLY, "failed", key, "未配置值班群号")
        return
    bot = try_get_bot()
    if bot is None:
        await db.set_task_state(TASK_GROUP_WEEKLY, "failed", key, "无可用机器人连接")
        return
    try:
        result = await broadcast_group_week(bot, monday, title="下周值班安排")
    except DutyApiError as e:
        logger.warning(f"群每周预告本期（{key}）接口异常: {e.message}")
        await db.set_task_state(TASK_GROUP_WEEKLY, "failed", key, e.message)
        return
    error = None if result.ok else result.error
    await db.set_task_state(
        TASK_GROUP_WEEKLY, "success" if result.ok else "failed", key, error
    )
    if result.ok:
        logger.info(f"群每周预告本期（{key}）发送成功")
    else:
        logger.warning(f"群每周预告本期（{key}）发送失败: {error}")


async def _job_tomorrow() -> None:
    """明日值班播报：明日班次聚合名单（图片）。"""
    db = get_db()
    now = now_beijing()
    tomorrow = now.date() + timedelta(days=1)
    key = tomorrow.isoformat()
    state = await db.get_task_state(TASK_TOMORROW)
    if state and state.status == "success" and state.business_key == key:
        logger.debug(f"明日播报本期（{key}）已成功，跳过")
        return
    if not _group_ready():
        await db.set_task_state(TASK_TOMORROW, "failed", key, "未配置值班群号")
        return
    bot = try_get_bot()
    if bot is None:
        await db.set_task_state(TASK_TOMORROW, "failed", key, "无可用机器人连接")
        return
    try:
        result = await broadcast_group_day(
            bot, tomorrow, title="明日值班安排", announce_empty=False
        )
    except DutyApiError as e:
        logger.warning(f"明日播报本期（{key}）接口异常: {e.message}")
        await db.set_task_state(TASK_TOMORROW, "failed", key, e.message)
        return
    error = None if result.ok else result.error
    await db.set_task_state(
        TASK_TOMORROW, "success" if result.ok else "failed", key, error
    )
    if result.ok:
        logger.info(f"明日播报本期（{key}）发送成功")
    else:
        logger.warning(f"明日播报本期（{key}）发送失败: {error}")


# ---------- 任务注册 ----------


def register_jobs(scheduler) -> None:
    """注册全部定时任务（北京时间、不并发、容忍窗口 5 分钟）。"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        _job_remind_poll,
        IntervalTrigger(minutes=plugin_config.dutybot_remind_interval, timezone=BEIJING),
        id=JOB_REMIND_POLL,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_MISFIRE_GRACE,
        replace_existing=True,
    )
    for job_id, cron in (
        (JOB_PERSONAL_WEEKLY, plugin_config.dutybot_personal_weekly_cron),
        (JOB_GROUP_WEEKLY, plugin_config.dutybot_group_weekly_cron),
        (JOB_TOMORROW, plugin_config.dutybot_tomorrow_cron),
    ):
        func = {
            JOB_PERSONAL_WEEKLY: _job_personal_weekly,
            JOB_GROUP_WEEKLY: _job_group_weekly,
            JOB_TOMORROW: _job_tomorrow,
        }[job_id]
        scheduler.add_job(
            func,
            CronTrigger.from_crontab(cron, timezone=BEIJING),
            id=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_MISFIRE_GRACE,
            replace_existing=True,
        )
    logger.info(
        f"已注册定时任务: 提醒轮询(每{plugin_config.dutybot_remind_interval}分钟), "
        f"个人每周预告({plugin_config.dutybot_personal_weekly_cron}), "
        f"群每周预告({plugin_config.dutybot_group_weekly_cron}), "
        f"明日播报({plugin_config.dutybot_tomorrow_cron})"
    )
