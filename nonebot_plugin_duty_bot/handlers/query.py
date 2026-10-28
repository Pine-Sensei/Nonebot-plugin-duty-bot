"""R2 私聊查询与 R3 群查询命令。"""

from __future__ import annotations

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.matcher import Matcher

from ..api import DutyApiError, get_client
from ..config import plugin_config
from ..render import (
    build_group_day_view,
    build_personal_view,
    deliver_view,
    format_date_cn,
    group_day_text_chunks,
    personal_text_chunks,
)
from ..sender import CAT_COMMAND, CHAT_GROUP, CHAT_PRIVATE
from ..storage import get_db
from ..timeutil import now_beijing
from .common import BIND_GUIDE, api_error_reply, log_usage, reply

my_roster_cmd = on_command("我的排班", priority=10, block=True)
today_cmd = on_command("今日值班", priority=10, block=True)


def _today_text(assignments) -> str:
    """今日班次纯文本。"""
    lines = []
    for a in sorted(assignments, key=lambda x: (x.start_time, x.end_time)):
        role = "主值" if a.role == "primary" else "备值"
        lines.append(f"· {a.start_time}-{a.end_time} {a.post_name}（{role}）")
    return "\n".join(lines)


@my_roster_cmd.handle()
async def _handle_my_roster(matcher: Matcher, bot: Bot, event) -> None:
    """我的排班（仅私聊）：全部有效已发布班次，图片输出。"""
    if not isinstance(event, PrivateMessageEvent):
        await matcher.finish()  # 个人数据仅私聊触达
        return
    log_usage(event, "我的排班")
    db = get_db()
    binding = await db.get_binding(qq=event.user_id)
    if binding is None:
        # 未绑定拦截：回复绑定引导，不调用排班接口
        await reply(matcher, bot, event, BIND_GUIDE)
        return
    try:
        roster = await get_client().get_roster(binding.student_no)
    except DutyApiError as e:
        logger.warning(f"我的排班查询接口异常: {e.message}")
        await reply(matcher, bot, event, api_error_reply())
        return
    view = build_personal_view(
        "我的排班", f"{binding.name}（{binding.student_no}）", roster.assignments
    )
    await deliver_view(
        bot,
        CHAT_PRIVATE,
        event.user_id,
        template_name="personal.html.j2",
        view=view,
        text_chunks=personal_text_chunks,
        category=CAT_COMMAND,
    )
    await matcher.finish()


@today_cmd.handle()
async def _handle_today(matcher: Matcher, bot: Bot, event) -> None:
    """今日值班：

    - 私聊：本人今日排班（纯文本）；
    - 配置群内：今日值班名单（图片，按班次聚合）；其他群不响应。
    """
    if isinstance(event, PrivateMessageEvent):
        log_usage(event, "今日值班(私聊)")
        db = get_db()
        binding = await db.get_binding(qq=event.user_id)
        if binding is None:
            await reply(matcher, bot, event, BIND_GUIDE)
            return
        try:
            roster = await get_client().get_today(binding.student_no)
        except DutyApiError as e:
            logger.warning(f"今日值班查询接口异常: {e.message}")
            await reply(matcher, bot, event, api_error_reply())
            return
        if not roster.assignments:
            await reply(matcher, bot, event, "今日无排班")
            return
        await reply(matcher, bot, event, "【今日值班】\n" + _today_text(roster.assignments))
        return

    if isinstance(event, GroupMessageEvent):
        if event.group_id != plugin_config.dutybot_group_id:
            await matcher.finish()  # 仅配置群内响应
            return
        log_usage(event, "今日值班(群)")
        try:
            roster = await get_client().get_today()  # 整体查询，短时缓存
        except DutyApiError as e:
            logger.warning(f"群今日值班查询接口异常: {e.message}")
            await reply(matcher, bot, event, api_error_reply())
            return
        if not roster.assignments:
            await reply(matcher, bot, event, "今日无值班安排")
            return
        view = build_group_day_view(
            "今日值班安排", format_date_cn(now_beijing().date()), roster.assignments
        )
        await deliver_view(
            bot,
            CHAT_GROUP,
            event.group_id,
            template_name="group_day.html.j2",
            view=view,
            text_chunks=group_day_text_chunks,
            category=CAT_COMMAND,
        )
        await matcher.finish()
        return

    await matcher.finish()
