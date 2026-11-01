"""R4 管理员命令：/状态、/绑定列表、/手动播报、/帮助。

管理员为配置的 QQ 号白名单；权限判断仅基于 QQ 号，不依赖群身份。
管理命令仅私聊响应，群内一律静默（防止管理操作与用户信息在群内可见）。
（/解绑 QQ号 与 3 参数 /绑定 在 handlers/bind.py 中处理。）
"""

from __future__ import annotations

import time
from datetime import timedelta

from nonebot import get_bots, logger, on_command, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..api import DutyApiError, get_client
from ..config import plugin_config
from ..jobs import (
    JOB_DISPLAY,
    JOB_TASK_MAP,
    broadcast_group_day,
    broadcast_group_week,
)
from ..storage import get_db
from ..timeutil import (
    format_uptime,
    next_week_range,
    now_beijing,
    this_week_range,
)
from .common import (
    PROCESS_START,
    api_error_reply,
    is_admin,
    is_private,
    log_usage,
    reply,
    send,
)

status_cmd = on_command("状态", priority=10, block=True)
bind_list_cmd = on_command("绑定列表", priority=10, block=True)
manual_cmd = on_command("手动播报", priority=10, block=True)
help_cmd = on_command("帮助", priority=10, block=True)

_ADMIN_MENU = (
    "管理员命令：\n"
    "/状态 - 运行体检\n"
    "/绑定列表 - 查看全部绑定\n"
    "/绑定 QQ号 学号 姓名 - 人工绑定\n"
    "/解绑 QQ号 - 解除指定绑定\n"
    "/手动播报 今日|明日|本周|下周 - 立即向值班群播报指定类型值班安排"
)

_PRIVATE_UNBOUND_MENU = (
    "你还未绑定学号。可用命令：\n/绑定 学号 姓名 - 自助绑定（姓名须与排班一致）\n/帮助 - 查看本菜单"
)

_PRIVATE_BOUND_MENU = (
    "可用命令：\n/我的排班 - 查询本人全部排班（图片）\n/今日值班 - 查询今日排班\n"
    "/解绑 - 解除本人绑定\n/帮助 - 查看本菜单"
)

_GROUP_MENU = (
    "本群可用命令：\n/今日值班 - 查询今日值班名单（图片）\n个人查询与绑定请私聊我发送 /帮助"
)


@status_cmd.handle()
async def _handle_status(matcher: Matcher, bot: Bot, event) -> None:
    """运行体检：连接、接口、绑定数、任务执行结果与下次执行时间、进程时长。"""
    if not is_private(event):
        await matcher.finish()  # 管理命令仅私聊，群内一律静默
        return
    if not is_admin(event):
        log_usage(event, "状态(拒绝)")
        await reply(matcher, bot, event, "该命令仅限管理员使用。")
        return
    log_usage(event, "状态")

    lines = []
    bots = get_bots()
    lines.append(
        f"机器人连接：已连接 {len(bots)} 个账号" if bots else "机器人连接：未连接"
    )
    ok, message = await get_client().probe()
    lines.append(f"值班系统接口：{'正常' if ok else '异常'}（{message}）")
    lines.append(f"绑定用户数：{await get_db().count_bindings()}")

    require("nonebot_plugin_apscheduler")
    from nonebot_plugin_apscheduler import scheduler

    for job_id, display in JOB_DISPLAY.items():
        state = await get_db().get_task_state(JOB_TASK_MAP[job_id])
        job = scheduler.get_job(job_id)
        next_run = (
            job.next_run_time.strftime("%m-%d %H:%M")
            if job and job.next_run_time
            else "未注册"
        )
        if state is None:
            last = "尚未执行"
        else:
            mark = "成功" if state.status == "success" else "失败"
            last = f"{mark}（{state.ran_at}）"
            if state.error:
                last += f"：{state.error}"
        lines.append(f"{display}：最近一次 {last}；下次 {next_run}")

    lines.append(f"进程运行时长：{format_uptime(time.time() - PROCESS_START)}")
    await reply(matcher, bot, event, "\n".join(lines))


@bind_list_cmd.handle()
async def _handle_bind_list(matcher: Matcher, bot: Bot, event) -> None:
    """分段展示全部绑定。"""
    if not is_private(event):
        await matcher.finish()  # 绑定信息仅私聊输出，群内一律静默
        return
    if not is_admin(event):
        log_usage(event, "绑定列表(拒绝)")
        await reply(matcher, bot, event, "该命令仅限管理员使用。")
        return
    log_usage(event, "绑定列表")
    bindings = await get_db().all_bindings()
    if not bindings:
        await reply(matcher, bot, event, "暂无绑定。")
        return
    pages = [bindings[i : i + 5] for i in range(0, len(bindings), 5)]
    total = len(bindings)
    for idx, page in enumerate(pages, 1):
        entries = "\n".join(
            f"{no}. QQ {b.qq} | 学号 {b.student_no} | 姓名 {b.name} | 绑定于 {b.bound_at}"
            for no, b in enumerate(page, (idx - 1) * 5 + 1)
        )
        text = f"【绑定列表】{idx}/{len(pages)}（共 {total} 条）\n{entries}"
        await send(matcher, bot, event, text)
    await matcher.finish()


@manual_cmd.handle()
async def _handle_manual_broadcast(matcher: Matcher, bot: Bot, event, args = CommandArg()) -> None:
    """手动播报：立即向值班群发送指定类型的群播报。

    - 今日/明日：按当前时间推算当日/次日，实时查询并播报该日值班安排；
    - 本周/下周：按当前时间推算本周/下周（周一至周日），实时查询并播报该周值班安排；
    - 必须显式指定类型，无隐藏默认值；不读取、不写入任务状态，
      因此也不影响定时任务的幂等跳过。
    """
    if not is_private(event):
        await matcher.finish()  # 管理命令仅私聊，群内一律静默
        return
    if not is_admin(event):
        log_usage(event, "手动播报(拒绝)")
        await reply(matcher, bot, event, "该命令仅限管理员使用。")
        return
    log_usage(event, "手动播报")
    arg = args.extract_plain_text().strip()
    if arg not in ("今日", "明日", "本周", "下周"):
        await reply(matcher, bot, event, "用法：/手动播报 今日|明日|本周|下周")
        return
    if not plugin_config.dutybot_group_id:
        await reply(matcher, bot, event, "未配置值班群号，无法播报。")
        return

    now = now_beijing()
    logger.info(f"管理员手动播报: {arg}")
    try:
        if arg in ("今日", "明日"):
            day = now.date() if arg == "今日" else now.date() + timedelta(days=1)
            result = await broadcast_group_day(bot, day, title=f"{arg}值班安排")
        elif arg == "本周":
            day = this_week_range(now.date())[0]
            result = await broadcast_group_week(bot, day, title="本周值班安排")
        else:
            day = next_week_range(now.date())[0]
            result = await broadcast_group_week(bot, day, title="下周值班安排")
    except DutyApiError as e:
        logger.warning(f"手动播报接口异常: {e.message}")
        await reply(matcher, bot, event, api_error_reply())
        return
    key = day.isoformat()
    if result.ok:
        if arg in ("今日", "明日"):
            text = f"已手动播报{arg}值班安排（{key}）。"
        else:
            text = f"已手动播报{arg}值班安排（{key} 起）。"
        await reply(matcher, bot, event, text)
    else:
        await reply(matcher, bot, event, f"播报失败：{result.error}")


@help_cmd.handle()
async def _handle_help(matcher: Matcher, bot: Bot, event) -> None:
    """帮助菜单：私聊按绑定状态/管理员返回对应菜单；群内返回通用菜单。"""
    log_usage(event, "帮助")
    if isinstance(event, GroupMessageEvent):
        if event.group_id != plugin_config.dutybot_group_id:
            await matcher.finish()  # 仅配置群内响应
            return
        text = _GROUP_MENU
    elif is_private(event):
        binding = await get_db().get_binding(qq=event.user_id)
        text = _PRIVATE_BOUND_MENU if binding else _PRIVATE_UNBOUND_MENU
    else:
        await matcher.finish()
        return
    if is_admin(event) and is_private(event):
        text += "\n\n" + _ADMIN_MENU  # 管理菜单仅私聊展示
    await reply(matcher, bot, event, text)
