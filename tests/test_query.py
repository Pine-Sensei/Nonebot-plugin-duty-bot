"""R2 私聊查询与 R3 群查询测试（nonebug）。"""

from nonebug import App

from .conftest import (
    ADMIN_QQ,
    GROUP_ID,
    USER_QQ,
    make_assignment,
    ob,
    run_matcher,
)


async def test_my_roster_unbound_blocked(app: App, fake_api, make_events):
    """未绑定拦截：回复绑定引导，不调用排班接口。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.query import my_roster_cmd

    guide = (
        "你还未绑定学号，无法使用该功能。\n"
        "请先发送 /绑定 学号 姓名 完成绑定（姓名须与排班中一致）；\n"
        "无排班学生请联系管理员人工绑定。"
    )
    await run_matcher(
        app, my_roster_cmd, make_private(USER_QQ, "/我的排班"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob(guide)})],
    )
    assert fake_api.roster_calls == []


async def test_my_roster_image(app: App, fake_api, monkeypatch, make_events):
    """已绑定 /我的排班：图片输出。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot_plugin_duty_bot.storage import get_db
    from nonebot.adapters.onebot.v11 import MessageSegment

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    fake_api.roster.assignments = [
        make_assignment(),
        make_assignment(id="a-2", slot_id="s2", start_time="14:00", post_name="西门岗"),
    ]

    async def fake_render(template_name, view):
        return b"MYROSTER-PNG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.query import my_roster_cmd

    await run_matcher(
        app, my_roster_cmd, make_private(USER_QQ, "/我的排班"),
        [("send_private_msg", {"user_id": USER_QQ, "message": MessageSegment.image(b"MYROSTER-PNG")})],
    )
    assert fake_api.roster_calls == ["2025123456789"]


async def test_my_roster_fallback_on_render_error(app: App, fake_api, monkeypatch, make_events):
    """渲染失败 → 纯文本分段降级。"""
    from nonebot_plugin_duty_bot import render as render_mod

    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    fake_api.roster.assignments = [make_assignment()]

    async def fake_render(template_name, view):
        raise RuntimeError("render boom")

    monkeypatch.setattr(render_mod, "render_template", fake_render)
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.query import my_roster_cmd
    from nonebot_plugin_duty_bot.render import build_personal_view, personal_text_chunks

    view = build_personal_view("我的排班", "张三（2025123456789）", fake_api.roster.assignments)
    chunks = personal_text_chunks(view)
    expect = [("send_private_msg", {"user_id": USER_QQ, "message": ob(c)}) for c in chunks]
    await run_matcher(app, my_roster_cmd, make_private(USER_QQ, "/我的排班"), expect)


async def test_my_roster_in_group_silent(app: App, fake_api, make_events):
    """个人数据仅私聊触达：群内 /我的排班 不响应。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.query import my_roster_cmd

    await run_matcher(app, my_roster_cmd, make_group(USER_QQ, GROUP_ID, "/我的排班"), [])
    assert fake_api.roster_calls == []


async def test_today_private_bound(app: App, fake_api, make_events):
    """私聊 /今日值班：本人今日排班纯文本。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    fake_api.today_roster.assignments = [
        make_assignment(start_time="08:00", end_time="10:00", post_name="东门岗"),
        make_assignment(id="a-2", slot_id="s2", start_time="14:00", end_time="16:00", post_name="西门岗"),
    ]
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.query import today_cmd

    text = "【今日值班】\n· 08:00-10:00 东门岗\n· 14:00-16:00 西门岗"
    await run_matcher(
        app, today_cmd, make_private(USER_QQ, "/今日值班"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob(text)})],
    )
    assert fake_api.today_calls == ["2025123456789"]


async def test_today_private_no_duty(app: App, fake_api, make_events):
    """私聊 /今日值班：今日无排班。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    fake_api.today_roster.assignments = []
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.query import today_cmd

    await run_matcher(
        app, today_cmd, make_private(USER_QQ, "/今日值班"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("今日无排班")})],
    )


async def test_today_group_image(app: App, fake_api, monkeypatch, make_events):
    """配置群内 /今日值班：整体查询 + 班次聚合图片。"""
    from nonebot_plugin_duty_bot import render as render_mod
    from nonebot.adapters.onebot.v11 import MessageSegment

    fake_api.today_roster.assignments = [
        make_assignment(slot_id="s1", start_time="08:00", post_name="东门岗", name="张三"),
        make_assignment(id="a-2", slot_id="s1", start_time="08:00", post_name="东门岗", name="李四"),
        make_assignment(id="a-3", slot_id="s2", start_time="14:00", post_name="西门岗", name="王五"),
    ]

    async def fake_render(template_name, view):
        assert template_name == "group_day.html.j2"
        return b"TODAY-GROUP-PNG"

    monkeypatch.setattr(render_mod, "render_template", fake_render)
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.query import today_cmd

    await run_matcher(
        app, today_cmd, make_group(USER_QQ, GROUP_ID, "/今日值班"),
        [("send_group_msg", {"group_id": GROUP_ID, "message": MessageSegment.image(b"TODAY-GROUP-PNG")})],
    )
    assert fake_api.today_calls == [None]  # 整体查询（无学号）


async def test_today_group_no_duty(app: App, fake_api, make_events):
    """配置群内 /今日值班：无班次纯文本。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.query import today_cmd

    await run_matcher(
        app, today_cmd, make_group(USER_QQ, GROUP_ID, "/今日值班"),
        [("send_group_msg", {"group_id": GROUP_ID, "message": ob("今日无值班安排")})],
    )


async def test_today_group_other_group_silent(app: App, fake_api, make_events):
    """非配置群内 /今日值班 不响应。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.query import today_cmd

    await run_matcher(app, today_cmd, make_group(USER_QQ, 99999, "/今日值班"), [])
    assert fake_api.today_calls == []
