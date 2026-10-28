"""R1 绑定命令测试（nonebug）。"""

from nonebug import App

from .conftest import (
    ADMIN_QQ,
    GROUP_ID,
    OTHER_QQ,
    USER_QQ,
    make_assignment,
)


def ob(text: str):
    """构造 OneBot V11 消息期望。"""
    from nonebot.adapters.onebot.v11 import Message

    return Message(text)


async def _run(app: App, matcher, event, expect: list):
    """通用执行：注册 API 期望后接收事件。"""
    from nonebot.adapters.onebot.v11 import Adapter, Bot
    import nonebot

    async with app.test_matcher(matcher) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        ctx.receive_event(bot, event)
        for api_name, data in expect:
            ctx.should_call_api(api_name, data, result={"message_id": 1})
        ctx.should_finished(matcher)


async def test_self_bind_success(app: App, fake_api, make_events):
    """核对成功：有排班且姓名一致 → 建立绑定并回复确认。"""
    fake_api.roster.assignments = [make_assignment(name="张三", student_no="2025123456789")]
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 2025123456789 张三"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("绑定成功！\n学号：2025123456789\n姓名：张三")})],
    )

    from nonebot_plugin_duty_bot.storage import get_db

    binding = await get_db().get_binding(qq=USER_QQ)
    assert binding is not None
    assert binding.student_no == "2025123456789"
    assert binding.name == "张三"
    assert fake_api.roster_calls == ["2025123456789"]


async def test_self_bind_no_roster(app: App, fake_api, make_events):
    """学号无已发布排班 → 提示无法自动核对。"""
    fake_api.roster.assignments = []
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 2025123456789 张三"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("无法自动核对，请联系管理员人工绑定。")})],
    )


async def test_self_bind_name_mismatch(app: App, fake_api, make_events):
    """排班姓名与提交姓名不一致 → 拒绝。"""
    fake_api.roster.assignments = [make_assignment(name="张三")]
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 2025123456789 李四"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("绑定失败：排班中的姓名与提交的姓名不一致，请核对后重试。")})],
    )


async def test_self_bind_qq_already_bound(app: App, fake_api, make_events):
    """该 QQ 已绑定其他学号 → 拒绝并说明占用情况，不调用接口。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 2025402011236 张三"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("绑定失败：你已绑定学号 2025123456789，如需更换请先发送 /解绑。")})],
    )
    assert fake_api.roster_calls == []


async def test_self_bind_student_occupied(app: App, fake_api, make_events):
    """学号已被其他 QQ 绑定 → 拒绝。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(OTHER_QQ, "2025123456789", "张三")
    fake_api.roster.assignments = [make_assignment(name="张三")]
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 2025123456789 张三"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("绑定失败：该学号已被其他 QQ 绑定，如有疑问请联系管理员。")})],
    )


async def test_bind_two_args_in_group_is_silent(app: App, fake_api, make_events):
    """2 参数绑定在群内不响应（个人自助绑定仅限私聊）。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(app, bind_cmd, make_group(USER_QQ, GROUP_ID, "/绑定 2025123456789 张三"), [])


async def test_admin_manual_bind_in_group_is_silent(app: App, fake_api, make_events):
    """3 参数绑定在群内一律静默：管理员与非管理员均不回复，也不落库。"""
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(app, bind_cmd, make_group(ADMIN_QQ, GROUP_ID, "/绑定 20001 2025123456789 张三"), [])
    await _run(app, bind_cmd, make_group(USER_QQ, GROUP_ID, "/绑定 20002 2025123456789 张三"), [])

    from nonebot_plugin_duty_bot.storage import get_db

    assert await get_db().get_binding(qq=20001) is None
    assert await get_db().get_binding(qq=20002) is None


async def test_bind_superscript_qq_shows_usage(app: App, fake_api, make_events):
    """QQ 参数为上标数字（"²"）→ 按用法提示处理，不异常终止。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    usage = (
        "用法：\n/绑定 学号 姓名 - 自助绑定（私聊）\n（管理员）/绑定 QQ号 学号 姓名 - 人工绑定"
    )
    await _run(
        app,
        bind_cmd,
        make_private(ADMIN_QQ, "/绑定 ² 2025123456789 张三"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(usage)})],
    )


async def test_admin_manual_bind(app: App, fake_api, make_events):
    """管理员 3 参数人工绑定（无需接口核对）。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(ADMIN_QQ, "/绑定 20001 2025123456789 张三"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob("人工绑定成功：\nQQ：20001\n学号：2025123456789\n姓名：张三")})],
    )

    from nonebot_plugin_duty_bot.storage import get_db

    assert await get_db().get_binding(qq=20001) is not None
    assert fake_api.roster_calls == []


async def test_admin_manual_bind_non_admin_rejected(app: App, fake_api, make_events):
    """非管理员 3 参数 → 无权限拒绝。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import bind_cmd

    await _run(
        app,
        bind_cmd,
        make_private(USER_QQ, "/绑定 20002 2025123456789 张三"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )


async def test_unbind_self(app: App, fake_api, make_events):
    """已绑定用户解绑本人；未绑定用户得到引导。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import unbind_cmd

    await _run(
        app,
        unbind_cmd,
        make_private(USER_QQ, "/解绑"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("已解除绑定（学号 2025123456789）。")})],
    )
    assert await get_db().get_binding(qq=USER_QQ) is None

    await _run(
        app,
        unbind_cmd,
        make_private(USER_QQ, "/解绑"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("你尚未绑定学号，无需解绑。发送 /绑定 学号 姓名 进行绑定。")})],
    )


async def test_admin_unbind_by_qq(app: App, fake_api, make_events):
    """/解绑 QQ号：管理员删除指定绑定；非管理员拒绝。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import unbind_cmd

    await _run(
        app,
        unbind_cmd,
        make_private(USER_QQ, f"/解绑 {USER_QQ}"),
        [("send_private_msg", {"user_id": USER_QQ, "message": ob("该命令仅限管理员使用。")})],
    )

    await _run(
        app,
        unbind_cmd,
        make_private(ADMIN_QQ, f"/解绑 {USER_QQ}"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob(f"已解除 QQ {USER_QQ} 的绑定。")})],
    )
    assert await get_db().get_binding(qq=USER_QQ) is None


async def test_unbind_in_group_is_silent(app: App, fake_api, make_events):
    """/解绑 在群内一律静默（含管理员带参与自助无参），不执行解绑。"""
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    _, make_group = make_events
    from nonebot_plugin_duty_bot.handlers.bind import unbind_cmd

    await _run(app, unbind_cmd, make_group(ADMIN_QQ, GROUP_ID, f"/解绑 {USER_QQ}"), [])
    await _run(app, unbind_cmd, make_group(USER_QQ, GROUP_ID, "/解绑"), [])
    assert await get_db().get_binding(qq=USER_QQ) is not None


async def test_unbind_superscript_qq_shows_usage(app: App, fake_api, make_events):
    """/解绑 QQ号 参数为上标数字（"²"）→ 用法提示，不异常终止。"""
    make_private, _ = make_events
    from nonebot_plugin_duty_bot.handlers.bind import unbind_cmd

    await _run(
        app,
        unbind_cmd,
        make_private(ADMIN_QQ, "/解绑 ²"),
        [("send_private_msg", {"user_id": ADMIN_QQ, "message": ob("用法：/解绑 QQ号")})],
    )
