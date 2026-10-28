"""R4 管理员命令测试（nonebug）：状态、绑定列表、重播、帮助。"""

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


# ---------- /重播 ----------


async def test_replay_requires_admin(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(USER_QQ, "/重播"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )


async def test_replay_in_group_silent(app: App, make_events):
    """群内 /重播 一律静默（管理员也不响应，不回复拒绝文案）。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(app, replay_cmd, make_group(ADMIN_QQ, GROUP_ID, "/重播"), [])
    await run_matcher(app, replay_cmd, make_group(USER_QQ, GROUP_ID, "/重播"), [])


async def test_replay_nothing_failed(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, "/重播"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob("当前没有发送失败的群播报，无需重播。")})],
    )


async def test_replay_invalid_arg(app: App, make_events):
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, "/重播 昨天"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob("用法：/重播 [明日|每周]")})],
    )


async def test_replay_success_state_no_need(app: App, fake_api, make_events):
    """本期已成功 → 无需重播。"""
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    key = (now_beijing().date() + timedelta(days=1)).isoformat()
    await get_db().set_task_state(TASK_TOMORROW, "success", key, None)

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, f"/重播 明日"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"本期（{key}）已成功发送，无需重播。")})],
    )


async def test_replay_tomorrow_broadcast(app: App, fake_api, monkeypatch, make_events):
    """补发最近一期失败的明日播报：按业务键重查重渲染，成功后更新任务状态。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"REPLAY-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    key = (now_beijing().date() + timedelta(days=1)).isoformat()
    await get_db().set_task_state(TASK_TOMORROW, "failed", key, "无可用机器人连接")
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, f"/重播 明日"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"REPLAY-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已补发本期（{key}）群播报。")}),
        ],
    )
    state = await get_db().get_task_state(TASK_TOMORROW)
    assert state.status == "success" and state.business_key == key


async def test_replay_weekly_broadcast(app: App, fake_api, monkeypatch, make_events):
    """补发最近一期失败的每周群播报：按业务键重查重渲染，成功后更新任务状态。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.storage import TASK_GROUP_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing, this_week_range
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"REPLAY-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    key = this_week_range(now_beijing().date())[0].isoformat()
    await get_db().set_task_state(TASK_GROUP_WEEKLY, "failed", key, "无可用机器人连接")
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, "/重播 每周"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"REPLAY-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已补发本期（{key}）群播报。")}),
        ],
    )
    state = await get_db().get_task_state(TASK_GROUP_WEEKLY)
    assert state.status == "success" and state.business_key == key


async def test_replay_auto_picks_recent_failure(app: App, fake_api, monkeypatch, make_events):
    """无参数：仅明日播报失败时自动补发该期。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"REPLAY-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    key = (now_beijing().date() + timedelta(days=1)).isoformat()
    await get_db().set_task_state(TASK_TOMORROW, "failed", key, "发送失败")
    fake_api.roster.assignments = [make_assignment(date=key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, "/重播"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"REPLAY-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已补发本期（{key}）群播报。")}),
        ],
    )


async def test_replay_auto_picks_most_recent_failure(app: App, fake_api, monkeypatch, make_events):
    """无参数且明日与每周均失败 → 选 ran_at 更近的一期补发。"""
    from nonebot_plugin_duty_bot import render as render_mod, storage
    from nonebot_plugin_duty_bot.storage import TASK_GROUP_WEEKLY, TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing, this_week_range
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def fake_render(template_name, view):
        return b"REPLAY-IMG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)

    # 明日播报 12:00 失败，每周播报 13:00 失败 → 应补发每周
    tomorrow_key = (now_beijing().date() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(storage, "_now_iso", lambda: "2026-10-27T12:00:00")
    await get_db().set_task_state(TASK_TOMORROW, "failed", tomorrow_key, "发送失败")
    weekly_key = this_week_range(now_beijing().date())[0].isoformat()
    monkeypatch.setattr(storage, "_now_iso", lambda: "2026-10-27T13:00:00")
    await get_db().set_task_state(TASK_GROUP_WEEKLY, "failed", weekly_key, "发送失败")

    fake_api.roster.assignments = [make_assignment(date=weekly_key)]

    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.admin import replay_cmd

    await run_matcher(
        app, replay_cmd, make_private(ADMIN_QQ, "/重播"),
        [
            ("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"REPLAY-IMG")}),
            ("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已补发本期（{weekly_key}）群播报。")}),
        ],
    )
    state = await get_db().get_task_state(TASK_GROUP_WEEKLY)
    assert state.status == "success" and state.business_key == weekly_key
    other = await get_db().get_task_state(TASK_TOMORROW)
    assert other.status == "failed" and other.business_key == tomorrow_key


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
        "/重播 [明日|每周] - 补发发送失败的群播报"
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
