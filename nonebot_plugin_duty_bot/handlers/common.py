"""事件处理公共工具。"""

from __future__ import annotations

import time

from nonebot import logger
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.matcher import Matcher

from ..api import DutyApiError, get_client
from ..config import plugin_config
from ..sender import CHAT_GROUP, CHAT_PRIVATE, SendResult, get_queue
from ..storage import BindConflictError, get_db

# 插件加载时刻，用于「状态」展示进程运行时长
PROCESS_START = time.time()

BIND_GUIDE = (
    "你还未绑定学号，无法使用该功能。\n"
    "请先发送 /绑定 学号 姓名 完成绑定（姓名须与排班中一致）；\n"
    "无排班学生请联系管理员人工绑定。"
)


def is_admin(event: Event) -> bool:
    """管理员白名单判断（仅基于 QQ 号）。"""
    try:
        return int(event.get_user_id()) in plugin_config.dutybot_admins
    except (ValueError, TypeError):
        return False


def is_private(event: Event) -> bool:
    """判断是否私聊消息事件。"""
    return isinstance(event, PrivateMessageEvent)


def is_qq_number(text: str) -> bool:
    """QQ 号参数校验：仅接受 ASCII 数字。

    str.isdigit() 会放行 Unicode 上标数字（如 "²"），int() 转换时抛 ValueError。
    """
    return text.isascii() and text.isdecimal()


def chat_info(event: Event) -> tuple[str, int]:
    """返回 (会话类型, 目标 ID)。"""
    if isinstance(event, PrivateMessageEvent):
        return CHAT_PRIVATE, event.user_id
    if isinstance(event, GroupMessageEvent):
        return CHAT_GROUP, event.group_id
    raise ValueError("不支持的事件类型")


async def send(matcher: Matcher, bot: Bot, event: Event, message) -> SendResult:
    """经发送队列发送一条消息（不结束会话）。"""
    chat_type, target_id = chat_info(event)
    return await get_queue().enqueue(bot, chat_type, target_id, message, "command")


async def reply(matcher: Matcher, bot: Bot, event: Event, message) -> None:
    """经发送队列回复并结束会话（回复实际发送结果为准）。"""
    await send(matcher, bot, event, message)
    await matcher.finish()


def log_usage(event: Event, command: str) -> None:
    """记录命令使用日志。"""
    try:
        chat_type, target_id = chat_info(event)
        logger.info(
            f"命令[{command}] 用户 {event.get_user_id()} 于 {chat_type}({target_id})"
        )
    except ValueError:
        pass


def conflict_message(e: BindConflictError) -> str:
    """将绑定冲突映射为明确的用户提示。"""
    if e.qq is not None:
        return "绑定失败：该 QQ 已绑定其他学号"
    if e.student_no is not None:
        return "绑定失败：该学号已被其他 QQ 绑定，如有疑问请联系管理员"
    return "绑定失败：学号或 QQ 已被占用，请稍后重试或联系管理员"


def api_error_reply() -> str:
    """接口异常统一用户提示（不暴露内部错误）。"""
    return "查询失败，请稍后重试；若持续失败请联系管理员。"


__all__ = [
    "BIND_GUIDE",
    "PROCESS_START",
    "DutyApiError",
    "api_error_reply",
    "chat_info",
    "conflict_message",
    "get_client",
    "get_db",
    "is_admin",
    "is_private",
    "is_qq_number",
    "log_usage",
    "reply",
    "send",
]
