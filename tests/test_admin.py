"""R4 管理员命令测试（nonebug）：状态、绑定列表、手动播报、帮助。"""

import types
from datetime import timedelta

from nonebug import App

from .conftest import ADMIN_QQ, GROUP_ID, USER_QQ, make_assignment, ob, run_matcher


# ---------- /状态 ----------


async def test_status_requires_admin(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import status_cmd

    await run_matcher(
        app, status_cmd, make_private(USER_QQ, "/状态"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )


async def test_status_content(app: App, fake_api, monkeypatch, make_events):
    """/状态 输出：连接、接口探测、绑定数、任务状态与下次执行、进程时长。"""
    import nonebot_plugin_apscheduler
    import nonebot_plugin_duty_bot.handlers.admin as admin_mod

    monkeypatch.setattr(admin_mod, "get_bots", lambda: {})
    monkeypatch.setattr(
        nonebot_plugin_apscheduler.scheduler, "get_job", lambda job_id: None
    )
    # 固定进程时长为 01:01:01
    monkeypatch.setattr(
        admin_mod, "time", types.SimpleNamespace(time=lambda: admin_mod.PROCESS_START + 3661)
    )
    fake_api.probe_result = (True, "正常")

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import status_cmd

    expected = "\n".join(
        [
            "机器人连接：未连接",
            "值班系统接口：正常（正常）",
            "绑定用户数：0",
            "值班前提醒轮询：最近一次 尚未执行；下次 未注册",
            "个人每周排班预告：最近一次 尚未执行；下次 未注册",
            "群每周排班预告：最近一次 尚未执行；下次 未注册",
            "明日值班播报：最近一次 尚未执行；下次 未注册",
            "进程运行时长：01:01:01",
        ]
    )
    await run_matcher(
        app, status_cmd, make_private(ADMIN_QQ, "/状态"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(expected)})],
    )
    assert fake_api.probe_calls == 1


async def test_status_in_group_silent(app: App, fake_api, make_events):
    """群内 /状态 一律静默（管理员与非管理员均不回复、不调用接口）。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.admin import status_cmd

    await run_matcher(app, status_cmd, make_group(ADMIN_QQ, GROUP_ID, "/状态"), [])
    await run_matcher(app, status_cmd, make_group(USER_QQ, GROUP_ID, "/状态"), [])
    assert fake_api.probe_calls == 0


# ---------- /绑定列表 ----------


async def test_bind_list(app: App, monkeypatch, make_events):
    """/绑定列表：分段展示全部绑定。"""
    from nonebot_plugin_duty_bot import storage

    monkeypatch.setattr(storage, "_now_iso", lambda: "2026-10-27T12:00:00")
    for i in range(1, 7):
        await storage.get_db().bind(20000 + i, f"202540201123{i}", f"同学{i}")

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import bind_list_cmd

    def page(idx: int, numbers: list[int]) -> str:
        entries = "\n".join(
            f"{no}. QQ {20000 + no} | 学号 202540201123{no} | 姓名 同学{no} | 绑定于 2026-10-27T12:00:00"
            for no in numbers
        )
        return f"【绑定列表】{idx}/2（共 6 条）\n{entries}"

    expected = [
        ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(page(1, [1, 2, 3, 4, 5]))}),
        ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(page(2, [6]))}),
    ]
    await run_matcher(app, bind_list_cmd, make_private(ADMIN_QQ, "/绑定列表"), expected)


async def test_bind_list_empty(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import bind_list_cmd

    await run_matcher(
        app, bind_list_cmd, make_private(ADMIN_QQ, "/绑定列表"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob("暂无绑定。")})],
    )


async def test_bind_list_non_admin_rejected(app: App, make_events):
    """私聊非管理员 /绑定列表 → 拒绝文案。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import bind_list_cmd

    await run_matcher(
        app, bind_list_cmd, make_private(USER_QQ, "/绑定列表"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )


async def test_bind_list_in_group_silent(app: App, make_events):
    """群内 /绑定列表 一律静默：防止用户 QQ/学号/姓名泄露到群聊。"""
    from nonebot_plugin_duty_bot import storage

    await storage.get_db().bind(USER_QQ, "2025123456789", "张三")
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.admin import bind_list_cmd

    await run_matcher(app, bind_list_cmd, make_group(ADMIN_QQ, GROUP_ID, "/绑定列表"), [])


# ---------- /手动播报 ----------


async def test_manual_requires_admin(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(USER_QQ, "/手动播报 明日"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )


async def test_manual_in_group_silent(app: App, make_events):
    """群内 /手动播报 一律静默（管理员也不响应，不回复拒绝文案）。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(app, manual_cmd, make_group(ADMIN_QQ, GROUP_ID, "/手动播报 明日"), [])
    await run_matcher(app, manual_cmd, make_group(USER_QQ, GROUP_ID, "/手动播报 明日"), [])


async def test_manual_requires_explicit_type(app: App, make_events):
    """无参数与非法参数（含旧的「每周」「当前周」）均提示用法：类型必须显式指定。"""
    usage = "用法：/手动播报 今日|明日|本周|下周"
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    for text in ("/手动播报", "/手动播报 昨天", "/手动播报 每周", "/手动播报 当前周"):
        await run_matcher(
            app, manual_cmd, make_private(ADMIN_QQ, text),
            [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(usage)})],
        )


async def test_manual_today(app: App, fake_api, monkeypatch, make_events):
    """/手动播报 今日：按当前时间推算当日，实时查询渲染发送。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.timeutil import now_beijing
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"MANUAL-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    key = now_beijing().date().isoformat()
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(ADMIN_QQ, "/手动播报 今日"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"MANUAL-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已手动播报今日值班安排（{key}）。")}),
        ],
    )


async def test_manual_tomorrow(app: App, fake_api, monkeypatch, make_events):
    """/手动播报 明日：按当前时间推算明天，实时查询渲染发送，不读写任务状态。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"MANUAL-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    key = (now_beijing().date() + timedelta(days=1)).isoformat()
    # 预置历史失败状态：手动播报不应读取或改写它
    await get_db().set_task_state(TASK_TOMORROW, "failed", "2000-01-01", "无可用机器人连接")
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(ADMIN_QQ, "/手动播报 明日"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"MANUAL-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已手动播报明日值班安排（{key}）。")}),
        ],
    )
    # 任务状态保持原样（不影响定时任务的幂等跳过）
    state = await get_db().get_task_state(TASK_TOMORROW)
    assert state.status == "failed" and state.business_key == "2000-01-01"


async def test_manual_current_week(app: App, fake_api, monkeypatch, make_events):
    """/手动播报 本周：按当前时间推算本周（周一~周日），实时查询渲染发送。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.timeutil import now_beijing, this_week_range
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"MANUAL-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    monday, _ = this_week_range(now_beijing().date())
    key = monday.isoformat()
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(ADMIN_QQ, "/手动播报 本周"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"MANUAL-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已手动播报本周值班安排（{key} 起）。")}),
        ],
    )


async def test_manual_next_week(app: App, fake_api, monkeypatch, make_events):
    """/手动播报 下周：按当前时间推算下周（周一~周日），实时查询渲染发送。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.timeutil import next_week_range, now_beijing
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"MANUAL-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    monday, _ = next_week_range(now_beijing().date())
    key = monday.isoformat()
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(ADMIN_QQ, "/手动播报 下周"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"MANUAL-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已手动播报下周值班安排（{key} 起）。")}),
        ],
    )


async def test_manual_tomorrow_empty_roster(app: App, fake_api, make_events):
    """明日无排班：向群发送文本提示并回复成功，便于管理员确认结果。"""
    from nonebot_plugin_duty_bot.timeutil import format_date_cn, now_beijing

    day = now_beijing().date() + timedelta(days=1)
    key = day.isoformat()

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import manual_cmd

    await run_matcher(
        app, manual_cmd, make_private(ADMIN_QQ, "/手动播报 明日"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": ob(f"明日值班安排：{format_date_cn(day)} 无值班安排")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已手动播报明日值班安排（{key}）。")}),
        ],
    )


# ---------- /帮助 ----------


async def test_help_private_unbound(app: App, fake_api, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import help_cmd

    expected = (
        "你还未绑定学号。可用命令：\n/绑定 学号 姓名 - 自助绑定（姓名须与排班一致）\n/帮助 - 查看本菜单"
    )
    await run_matcher(
        app, help_cmd, make_private(USER_QQ, "/帮助"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob(expected)})],
    )


async def test_help_private_bound(app: App, fake_api, make_events):
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import help_cmd

    expected = (
        "可用命令：\n/我的排班 - 查询本人全部排班（图片）\n/今日值班 - 查询今日排班\n"
        "/解绑 - 解除本人绑定\n/帮助 - 查看本菜单"
    )
    await run_matcher(
        app, help_cmd, make_private(USER_QQ, "/帮助"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob(expected)})],
    )


async def test_help_private_admin(app: App, fake_api, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import help_cmd

    expected = (
        "你还未绑定学号。可用命令：\n/绑定 学号 姓名 - 自助绑定（姓名须与排班一致）\n/帮助 - 查看本菜单"
        "\n\n管理员命令：\n/状态 - 运行体检\n/绑定列表 - 查看全部绑定\n"
        "/绑定 QQ号 学号 姓名 - 人工绑定\n/解绑 QQ号 - 解除指定绑定\n"
        "/手动播报 今日|明日|本周|下周 - 立即向值班群播报指定类型值班安排"
    )
    await run_matcher(
        app, help_cmd, make_private(ADMIN_QQ, "/帮助"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(expected)})],
    )


async def test_help_group(app: App, fake_api, make_events):
    """群内返回通用菜单（管理员亦不附加管理菜单）；非配置群不响应。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.admin import help_cmd

    expected = (
        "本群可用命令：\n/今日值班 - 查询今日值班名单（图片）\n个人查询与绑定请私聊我发送 /帮助"
    )
    await run_matcher(
        app, help_cmd, make_group(USER_QQ, GROUP_ID, "/帮助"),
        [("send_group_msg", {"group_id": GROUP_ID, "message": ob(expected)})],
    )
    # 管理员在群内也只看到通用菜单
    await run_matcher(
        app, help_cmd, make_group(ADMIN_QQ, GROUP_ID, "/帮助"),
        [("send_group_msg", {"group_id": GROUP_ID, "message": ob(expected)})],
    )
    # 非配置群不响应
    await run_matcher(app, help_cmd, make_group(USER_QQ, 99999, "/帮助"), [])
