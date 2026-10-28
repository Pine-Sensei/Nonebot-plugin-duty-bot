"""pytest 全局配置：NoneBug 初始化、假 API 客户端、事件工厂与测试隔离。"""

import os
import tempfile
from datetime import datetime

import pytest
import nonebot
from pytest_asyncio import is_async_test

# NoneBot 初始化前注入测试配置（环境变量优先级高于 dotenv）
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="dutybot-test-")
os.environ.setdefault("DRIVER", "~fastapi+~httpx+~websockets")
os.environ.setdefault("COMMAND_START", '["/"]')
os.environ.setdefault("DUTYBOT_API_BASE_URL", "http://127.0.0.1:18787")
os.environ.setdefault("DUTYBOT_API_KEY", "test-key")
os.environ.setdefault("DUTYBOT_GROUP_ID", "10086")
os.environ.setdefault("DUTYBOT_ADMINS", '["10000"]')
os.environ.setdefault("LOCALSTORE_DATA_DIR", _TMP_DATA_DIR)
# 测试中限流调至极高（限流窗口跨全部测试共享；限流逻辑本身由 test_sender 注入时钟精确覆盖）
os.environ.setdefault("DUTYBOT_RATE_PRIVATE_PER_MIN", "1000")
os.environ.setdefault("DUTYBOT_RATE_GROUP_PER_MIN", "1000")

ADMIN_QQ = 10000
GROUP_ID = 10086
USER_QQ = 20001
OTHER_QQ = 20002


def pytest_collection_modifyitems(items: list[pytest.Item]):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope="session")
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None):
    """注册 OneBot V11 适配器并从 pyproject.toml 加载插件。

    必须以此名称覆盖 nonebug 内置的同名 fixture，使其成为
    `nonebug_init`（lifespan startup）的前置依赖；
    否则 startup 钩子会在插件加载前执行，导致初始化丢失。
    """
    from nonebot.adapters.onebot.v11 import Adapter

    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    nonebot.load_from_toml("pyproject.toml")


@pytest.fixture(autouse=True)
async def _reset_state():
    """测试后清理持久数据与内存状态，保证用例隔离。"""
    yield
    from nonebot_plugin_duty_bot import jobs, storage
    from nonebot_plugin_duty_bot.api import reset_client

    if storage.db is not None and storage.db.connected:
        await storage.db.clear_data()
    jobs._reminded.clear()
    reset_client()


# ---------- 假值班系统 API ----------


class FakeApi:
    """DutyApiClient 的假实现，行为由各测试配置。

    注意：所有插件相关导入必须在测试运行期（NoneBot 初始化后）执行，
    因此类型定义延迟到方法内部导入。
    """

    def __init__(self) -> None:
        from nonebot_plugin_duty_bot.api import Roster

        self.configured = True
        self.roster = Roster(date="2026-10-27")
        self.today_roster = Roster(date="2026-10-27")
        self.probe_result = (True, "正常")
        self.roster_calls: list = []
        self.today_calls: list = []
        self.probe_calls = 0

    async def get_roster(self, student_no=None):
        from nonebot_plugin_duty_bot.api import Roster

        self.roster_calls.append(student_no)
        assert isinstance(self.roster, Roster)
        return self.roster

    async def get_today(self, student_no=None):
        self.today_calls.append(student_no)
        return self.today_roster

    async def probe(self):
        self.probe_calls += 1
        return self.probe_result

    async def close(self) -> None:
        pass


@pytest.fixture()
def fake_api(monkeypatch):
    """用 FakeApi 替换全局 API 客户端。"""
    from nonebot_plugin_duty_bot import api

    fake = FakeApi()
    monkeypatch.setattr(api, "client", fake)
    return fake


# ---------- 数据构造工具 ----------


def make_assignment(**overrides):
    """构造排班分配记录，缺省为可用的明日白班。"""
    from nonebot_plugin_duty_bot.api import Assignment
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    defaults = dict(
        id="a-1",
        slot_id="s-1",
        student_no="2025123456789",
        name="张三",
        role="primary",
        version=0,
        active=1,
        cycle_id="c-1",
        cycle_name="第一周期",
        week_no=5,
        weekday=1,
        date=(now_beijing().date().isoformat()),
        start_time="08:00",
        end_time="10:00",
        post_name="东门岗",
        teammates=[],
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def make_slot(**overrides):
    from nonebot_plugin_duty_bot.api import Slot

    defaults = dict(
        slot_id="s-1",
        cycle_id="c-1",
        cycle_name="第一周期",
        week_no=5,
        weekday=1,
        date=None,
        start_time="08:00",
        end_time="10:00",
        post_name="东门岗",
        primary_need=1,
        backup_need=1,
    )
    defaults.update(overrides)
    return Slot(**defaults)


@pytest.fixture()
def make_events():
    """构造 OneBot V11 私聊/群消息事件。"""

    def _private(user_id: int, text: str):
        from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
        from nonebot.adapters.onebot.v11.event import Sender

        return PrivateMessageEvent(
            post_type="message",
            time=int(datetime.now().timestamp()),
            self_id="10001",
            sub_type="friend",
            user_id=user_id,
            message_type="private",
            message_id=1,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
            font=0,
            sender=Sender(user_id=user_id, nickname=f"user{user_id}"),
            to_me=True,
        )

    def _group(user_id: int, group_id: int, text: str):
        from nonebot.adapters.onebot.v11 import Message, GroupMessageEvent
        from nonebot.adapters.onebot.v11.event import Sender

        return GroupMessageEvent(
            post_type="message",
            time=int(datetime.now().timestamp()),
            self_id="10001",
            sub_type="normal",
            group_id=group_id,
            user_id=user_id,
            message_type="group",
            message_id=1,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
            font=0,
            sender=Sender(user_id=user_id, nickname=f"user{user_id}", card=""),
            to_me=False,
        )

    return _private, _group


# ---------- 通用 matcher 执行助手 ----------


def ob(text: str):
    """构造 OneBot V11 消息期望（发送队列会将 str 强转为 Message）。"""
    from nonebot.adapters.onebot.v11 import Message

    return Message(text)


async def run_matcher(app, matcher, event, expect: list) -> None:
    """接收事件并断言预期的平台 API 调用序列与结束状态。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, Bot
    from nonebug import App  # noqa: F401

    async with app.test_matcher(matcher) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter)
        ctx.receive_event(bot, event)
        for api_name, data in expect:
            ctx.should_call_api(api_name, data, result={"message_id": 1})
        ctx.should_finished(matcher)
