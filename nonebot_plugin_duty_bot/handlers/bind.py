"""R1 绑定命令（仅私聊）：/绑定（自助 2 参数 + 管理员人工 3 参数）、/解绑。"""

from __future__ import annotations

from nonebot import logger, on_command
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..api import DutyApiError
from ..storage import BindConflictError, get_db
from .common import (
    api_error_reply,
    conflict_message,
    get_client,
    is_admin,
    is_private,
    is_qq_number,
    log_usage,
    reply,
)

bind_cmd = on_command("绑定", priority=10, block=True)
unbind_cmd = on_command("解绑", priority=10, block=True)


@bind_cmd.handle()
async def _handle_bind(
    matcher: Matcher, bot: Bot, event, args: Message = CommandArg()
) -> None:
    """绑定命令（仅私聊，群内一律静默）按参数个数区分：

    - 3 参数：管理员人工绑定；
    - 2 参数：用户自助绑定（排班接口核对姓名）；
    - 其余参数个数：回复用法。
    """
    if not is_private(event):
        await matcher.finish()  # 绑定命令仅限私聊，群内不响应
        return
    text = args.extract_plain_text().strip()
    parts = text.split()
    if len(parts) == 3 and is_qq_number(parts[0]):
        await _admin_bind(matcher, bot, event, int(parts[0]), parts[1], parts[2])
        return
    if len(parts) == 2:
        await _self_bind(matcher, bot, event, parts[0], parts[1])
        return
    await reply(
        matcher,
        bot,
        event,
        "用法：\n/绑定 学号 姓名 - 自助绑定（私聊）\n（管理员）/绑定 QQ号 学号 姓名 - 人工绑定",
    )


async def _self_bind(
    matcher: Matcher, bot: Bot, event: PrivateMessageEvent, student_no: str, name: str
) -> None:
    """用户自助绑定：排班接口核对姓名后建立绑定。"""
    log_usage(event, "绑定")
    db = get_db()

    existing = await db.get_binding(qq=event.user_id)
    if existing is not None:
        await reply(
            matcher,
            bot,
            event,
            f"绑定失败：你已绑定学号 {existing.student_no}，如需更换请先发送 /解绑。",
        )
        return

    client = get_client()
    if not client.configured:
        await reply(matcher, bot, event, "值班系统接口未配置，请联系管理员。")
        return
    try:
        roster = await client.get_roster(student_no)
    except DutyApiError as e:
        logger.warning(f"绑定核对接口异常: {e.message}")
        await reply(matcher, bot, event, api_error_reply())
        return

    if not roster.assignments:
        await reply(matcher, bot, event, "无法自动核对，请联系管理员人工绑定。")
        return
    if name not in {a.name for a in roster.assignments}:
        await reply(
            matcher, bot, event, "绑定失败：排班中的姓名与提交的姓名不一致，请核对后重试。"
        )
        return

    occupant = await db.get_binding(student_no=student_no)
    if occupant is not None:
        await reply(
            matcher, bot, event, "绑定失败：该学号已被其他 QQ 绑定，如有疑问请联系管理员。"
        )
        return
    try:
        await db.bind(event.user_id, student_no, name)
    except BindConflictError:
        await reply(
            matcher,
            bot,
            event,
            "绑定失败：该学号或你的 QQ 刚刚被其他用户绑定，请重试或联系管理员。",
        )
        return
    logger.info(f"绑定成功: QQ {event.user_id} -> 学号 {student_no}")
    await reply(matcher, bot, event, f"绑定成功！\n学号：{student_no}\n姓名：{name}")


async def _admin_bind(
    matcher: Matcher, bot: Bot, event, qq: int, student_no: str, name: str
) -> None:
    """管理员人工绑定：兜底覆盖无排班学生，核对责任由管理员承担。"""
    if not is_admin(event):
        log_usage(event, "绑定(3参,拒绝)")
        await reply(matcher, bot, event, "该命令仅限管理员使用。")
        return
    log_usage(event, "绑定(人工)")
    try:
        await get_db().bind(qq, student_no, name)
    except BindConflictError as e:
        await reply(matcher, bot, event, conflict_message(e))
        return
    logger.info(f"管理员人工绑定: QQ {qq} -> 学号 {student_no}")
    await reply(
        matcher, bot, event, f"人工绑定成功：\nQQ：{qq}\n学号：{student_no}\n姓名：{name}"
    )


@unbind_cmd.handle()
async def _handle_unbind(
    matcher: Matcher, bot: Bot, event, args: Message = CommandArg()
) -> None:
    """解绑命令（仅私聊，群内一律静默）：

    - 带参数（/解绑 QQ号）：管理员解除指定绑定；
    - 无参数：已绑定用户解除本人绑定，未绑定用户得到引导提示。
    """
    if not is_private(event):
        await matcher.finish()  # 解绑命令仅限私聊，群内不响应
        return
    text = args.extract_plain_text().strip()
    parts = text.split()

    if parts:
        if not is_admin(event):
            log_usage(event, "解绑(带参,拒绝)")
            await reply(matcher, bot, event, "该命令仅限管理员使用。")
            return
        log_usage(event, "解绑(管理)")
        if not (len(parts) == 1 and is_qq_number(parts[0])):
            await reply(matcher, bot, event, "用法：/解绑 QQ号")
            return
        removed = await get_db().unbind(int(parts[0]))
        if removed:
            await reply(matcher, bot, event, f"已解除 QQ {parts[0]} 的绑定。")
        else:
            await reply(matcher, bot, event, f"QQ {parts[0]} 未绑定，无需解绑。")
        return

    log_usage(event, "解绑")
    db = get_db()
    binding = await db.get_binding(qq=event.user_id)
    if binding is None:
        await reply(
            matcher,
            bot,
            event,
            "你尚未绑定学号，无需解绑。发送 /绑定 学号 姓名 进行绑定。",
        )
        return
    await db.unbind(event.user_id)
    logger.info(f"解绑成功: QQ {event.user_id}（学号 {binding.student_no}）")
    await reply(matcher, bot, event, f"已解除绑定（学号 {binding.student_no}）。")
