"""R4 管理员命令：/状态、/绑定列表、/重播、/帮助。

管理员为配置的 QQ 号白名单；权限判断仅基于 QQ 号，不依赖群身份。
管理命令仅私聊响应，群内一律静默（防止管理操作与用户信息在群内可见）。
（/解绑 QQ号 与 3 参数 /绑定 在 handlers/bind.py 中处理。）
"""

from __future__ import annotations

import time
from datetime import date

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
from ..storage import (
    TASK_GROUP_WEEKLY,
    TASK_TOMORROW,
    get_db,
)
from ..timeutil import format_uptime
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
replay_cmd = on_command("重播", priority=10, block=True)
help_cmd = on_command("帮助", priority=10, block=True)

_ADMIN_MENU = (
    "管理员命令：\n"
    "/状态 - 运行体检\n"
    "/绑定列表 - 查看全部绑定\n"
    "/绑定 QQ号 学号 姓名 - 人工绑定\n"
    "/解绑 QQ号 - 解除指定绑定\n"
    "/重播 [明日|每周] - 补发发送失败的群播报"
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


@replay_cmd.handle()
async def _handle_replay(matcher: Matcher, bot: Bot, event, args = CommandArg()) -> None:
    """重播：补发最近一期发送失败的群播报。

    - 无参数：自动选两类群播报任务中最近一次失败者；
    - 明日/每周：指定类型补发该类型最近失败的一期；
    - 本期已成功时提示无需重播；重播按业务键重新查询当前数据并渲染发送。
    """
    if not is_private(event):
        await matcher.finish()  # 管理命令仅私聊，群内一律静默
        return
    if not is_admin(event):
        log_usage(event, "重播(拒绝)")
        await reply(matcher, bot, event, "该命令仅限管理员使用。")
        return
    log_usage(event, "重播")
    arg = args.extract_plain_text().strip()
    task_alias = {"明日": TASK_TOMORROW, "每周": TASK_GROUP_WEEKLY}
    if arg and arg not in task_alias:
        await reply(matcher, bot, event, "用法：/重播 [明日|每周]")
        return
    if not plugin_config.dutybot_group_id:
        await reply(matcher, bot, event, "未配置值班群号，无法补发。")
        return

    db = get_db()
    if arg:
        task_id = task_alias[arg]
    else:
        candidates: list[tuple[str, str]] = []
        for tid in (TASK_TOMORROW, TASK_GROUP_WEEKLY):
            state = await db.get_task_state(tid)
            if state and state.status != "success":
                candidates.append((state.ran_at, tid))
        if not candidates:
            await reply(matcher, bot, event, "当前没有发送失败的群播报，无需重播。")
            return
        candidates.sort()
        task_id = candidates[-1][1]

    state = await db.get_task_state(task_id)
    if state is None:
        await reply(matcher, bot, event, "该类型播报尚未执行过，无需重播。")
        return
    if state.status == "success":
        await reply(
            matcher, bot, event, f"本期（{state.business_key}）已成功发送，无需重播。"
        )
        return
    key = state.business_key
    if not key:
        await reply(matcher, bot, event, "该期失败记录缺少业务键，无法重播。")
        return

    logger.info(f"管理员重播: {task_id} 期 {key}")
    try:
        day = date.fromisoformat(key)
        if task_id == TASK_TOMORROW:
            result = await broadcast_group_day(bot, day, title="明日值班安排")
        else:
            result = await broadcast_group_week(bot, day, title="本周值班安排")
    except DutyApiError as e:
        logger.warning(f"重播接口异常: {e.message}")
        await reply(matcher, bot, event, api_error_reply())
        return
    except ValueError:
        await reply(matcher, bot, event, "业务键格式异常，无法重播。")
        return
    if result.ok:
        await db.set_task_state(task_id, "success", key, None)
        await reply(matcher, bot, event, f"已补发本期（{key}）群播报。")
    else:
        await reply(matcher, bot, event, f"补发失败：{result.error}")


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
