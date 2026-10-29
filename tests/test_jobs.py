"""定时任务测试：提醒轮询、每周预告、群播报、幂等与注册。"""

from datetime import timedelta

import pytest
from nonebug import App

from .conftest import GROUP_ID, OTHER_QQ, USER_QQ, make_assignment


class FakeBot:
    """记录 API 调用的假 Bot；可指定失败目标。"""

    def __init__(self, fail_users: set | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_users = fail_users or set()

    async def call_api(self, api: str, **kwargs):
        target = kwargs.get("user_id", kwargs.get("group_id"))
        if api == "send_private_msg" and target in self.fail_users:
            raise RuntimeError("mock private send failure")
        self.calls.append((api, kwargs))
        return {"message_id": len(self.calls)}

    @staticmethod
    def plain(kwargs) -> str:
        return kwargs["message"].extract_plain_text()


@pytest.fixture()
def patch_render(monkeypatch):
    """图片渲染桩，避免真实渲染依赖。"""
    from nonebot_plugin_duty_bot import render as render_mod

    async def fake_render(template_name, view):
        return template_name.encode()

    monkeypatch.setattr(render_mod, "render_template", fake_render)
    return fake_render


def _assignment_in_minutes(minutes_ahead: int = 30) -> dict:
    """生成开始时刻相对当前时间偏移 minutes_ahead 分钟的班次参数（负值=已开始）。"""
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    target = now_beijing() + timedelta(minutes=minutes_ahead)
    return {"date": target.date().isoformat(), "start_time": target.strftime("%H:%M")}


async def test_remind_poll_sends_and_dedups(app: App, fake_api, monkeypatch):
    """窗口内班次私聊提醒；同一班次不重复提醒。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_REMIND_POLL, get_db

    await get_db().bind(USER_QQ, "2025123456789", "张三")
    params = _assignment_in_minutes(30)
    fake_api.roster.assignments = [
        make_assignment(**params),
        # 日期未定 → 永不提醒
        make_assignment(id="a-h", date=None),
    ]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_remind_poll()
    private_calls = [c for c in bot.calls if c[0] == "send_private_msg"]
    assert len(private_calls) == 1
    text = FakeBot.plain(private_calls[0][1])
    assert "值班提醒" in text and "东门岗" in text
    state = await get_db().get_task_state(TASK_REMIND_POLL)
    assert state.status == "success"

    # 第二轮：已提醒不重复；窗口内新班次会提醒
    await jobs._job_remind_poll()
    private_calls = [c for c in bot.calls if c[0] == "send_private_msg"]
    assert len(private_calls) == 1


async def test_remind_poll_retry_after_failure(app: App, fake_api, monkeypatch):
    """发送失败回滚去重状态，下一轮轮询自然重试。"""
    from nonebot_plugin_duty_bot import jobs

    await _bind_user(USER_QQ)
    params = _assignment_in_minutes(30)
    fake_api.roster.assignments = [make_assignment(**params)]
    bot = FakeBot(fail_users={USER_QQ})
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_remind_poll()
    assert len(bot.calls) == 0  # 发送失败

    # 修复后重试成功
    bot2 = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot2)
    await jobs._job_remind_poll()
    assert len(bot2.calls) == 1


def test_try_get_bot_normalizes_no_bot(monkeypatch):
    """get_bot 无 Bot 时抛 ValueError（NoneBot 2.x 不传 self_id 的行为），try_get_bot 归一化为 None。

    注意：这里 mock 的是 get_bot 而非 try_get_bot 本身，确保被测函数内部逻辑被覆盖。
    """
    from nonebot_plugin_duty_bot import jobs

    def no_bot():
        raise ValueError("There are no bots to get.")

    monkeypatch.setattr(jobs, "get_bot", no_bot)
    assert jobs.try_get_bot() is None


async def test_remind_poll_no_bot(app: App, fake_api, monkeypatch):
    """无机器人连接：记录失败结果，不中断。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_REMIND_POLL, get_db

    monkeypatch.setattr(jobs, "try_get_bot", lambda: None)
    await jobs._job_remind_poll()
    state = await get_db().get_task_state(TASK_REMIND_POLL)
    assert state.status == "failed"
    assert "机器人" in (state.error or "")


async def test_remind_poll_no_bot_records_state_on_value_error(app: App, fake_api, monkeypatch):
    """不 mock try_get_bot：get_bot 抛 ValueError（真实无连接场景）时任务状态仍记录失败。

    回归防护：try_get_bot 曾只捕获 KeyError，导致该场景异常穿透、状态停留在上一期。
    """
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_REMIND_POLL, get_db

    def no_bot():
        raise ValueError("There are no bots to get.")

    monkeypatch.setattr(jobs, "get_bot", no_bot)
    await jobs._job_remind_poll()
    state = await get_db().get_task_state(TASK_REMIND_POLL)
    assert state.status == "failed"
    assert "机器人" in (state.error or "")


async def test_remind_poll_window_boundaries(app: App, fake_api, monkeypatch):
    """窗口边界：已开始班次（start < now）与超窗班次（start > now + 窗口）均不提醒。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.config import plugin_config
    from nonebot_plugin_duty_bot.storage import TASK_REMIND_POLL, get_db

    await _bind_user(USER_QQ)
    window = plugin_config.dutybot_remind_window
    fake_api.roster.assignments = [
        make_assignment(id="a-started", **_assignment_in_minutes(-10)),
        make_assignment(id="a-far", **_assignment_in_minutes(window + 30)),
    ]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_remind_poll()
    assert len(bot.calls) == 0
    state = await get_db().get_task_state(TASK_REMIND_POLL)
    assert state.status == "success"  # 无命中窗口不算失败


async def test_remind_poll_duplicate_once_after_restart(app: App, fake_api, monkeypatch):
    """重启后窗口内允许重复一次：清空内存去重（模拟重启）后同班次再提醒一次。"""
    from nonebot_plugin_duty_bot import jobs

    await _bind_user(USER_QQ)
    fake_api.roster.assignments = [make_assignment(id="a-1", **_assignment_in_minutes(30))]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_remind_poll()
    assert len(bot.calls) == 1

    jobs._reminded.clear()  # 去重为内存态，重启即清空
    await jobs._job_remind_poll()
    assert len(bot.calls) == 2

    await jobs._job_remind_poll()  # 不重启则继续去重
    assert len(bot.calls) == 2


async def test_personal_weekly(app: App, fake_api, monkeypatch, patch_render):
    """个人每周预告：下周班次私聊图片推送；无下周班次用户跳过。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_PERSONAL_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import next_week_range

    await _bind_user(USER_QQ, "2025123456789", "张三")
    await _bind_user(OTHER_QQ, "2025402011236", "李四")  # 无下周班次
    monday, sunday = next_week_range(_today())
    fake_api.roster.assignments = [
        make_assignment(id="1", date=monday.isoformat()),
        make_assignment(id="2", date=sunday.isoformat(), slot_id="s2", start_time="14:00"),
        make_assignment(id="3", date=None),  # 日期未定不参与
    ]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_personal_weekly()
    private_calls = [c for c in bot.calls if c[0] == "send_private_msg"]
    assert len(private_calls) == 1  # 仅 USER_QQ
    assert private_calls[0][1]["user_id"] == USER_QQ
    assert _seg_type(private_calls[0][1]["message"]) == "image"
    state = await get_db().get_task_state(TASK_PERSONAL_WEEKLY)
    assert state.status == "success"
    assert state.business_key == monday.isoformat()

    # 本期已成功 → 幂等跳过
    await jobs._job_personal_weekly()
    assert len(bot.calls) == 1


async def test_personal_weekly_partial_failure(app: App, fake_api, monkeypatch, patch_render):
    """部分用户发送失败：任务状态记失败（仅日志，无补发）。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_PERSONAL_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import next_week_range

    await _bind_user(USER_QQ, "2025123456789", "张三")
    await _bind_user(OTHER_QQ, "2025402011236", "李四")
    monday, sunday = next_week_range(_today())
    fake_api.roster.assignments = [
        make_assignment(id="1", date=monday.isoformat()),
        make_assignment(id="2", date=monday.isoformat(), student_no="2025402011236", slot_id="s2"),
    ]
    bot = FakeBot(fail_users={OTHER_QQ})
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_personal_weekly()
    state = await get_db().get_task_state(TASK_PERSONAL_WEEKLY)
    assert state.status == "failed"
    assert "1 人发送失败" in (state.error or "")


async def test_group_weekly(app: App, fake_api, monkeypatch, patch_render):
    """群每周预告：本周班次按日期分组图片播报，本周一为业务键。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_GROUP_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import this_week_range

    monday, sunday = this_week_range(_today())
    fake_api.roster.assignments = [
        make_assignment(id="1", date=monday.isoformat()),
        make_assignment(id="2", date=sunday.isoformat(), slot_id="s2", start_time="14:00"),
        make_assignment(id="3", date=None),
    ]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_group_weekly()
    group_calls = [c for c in bot.calls if c[0] == "send_group_msg"]
    assert len(group_calls) == 1
    assert group_calls[0][1]["group_id"] == GROUP_ID
    assert _seg_type(group_calls[0][1]["message"]) == "image"
    state = await get_db().get_task_state(TASK_GROUP_WEEKLY)
    assert state.status == "success" and state.business_key == monday.isoformat()

    await jobs._job_group_weekly()  # 幂等
    assert len(bot.calls) == 1


async def test_group_weekly_empty_week(app: App, fake_api, monkeypatch):
    """回归：本周无班次时纯文本播报并正确落库（曾漏传 category 致 TypeError、状态不落库）。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_GROUP_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import this_week_range

    fake_api.roster.assignments = []
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_group_weekly()
    group_calls = [c for c in bot.calls if c[0] == "send_group_msg"]
    assert len(group_calls) == 1
    assert group_calls[0][1]["group_id"] == GROUP_ID
    assert "本周无值班安排" in FakeBot.plain(group_calls[0][1])
    state = await get_db().get_task_state(TASK_GROUP_WEEKLY)
    monday, _ = this_week_range(_today())
    assert state.status == "success" and state.business_key == monday.isoformat()


async def test_tomorrow_broadcast(app: App, fake_api, monkeypatch, patch_render):
    """明日播报：明日班次图片；无班次纯文本；幂等。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    tomorrow = now_beijing().date() + timedelta(days=1)
    fake_api.roster.assignments = [make_assignment(id="1", date=tomorrow.isoformat())]
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_tomorrow()
    group_calls = [c for c in bot.calls if c[0] == "send_group_msg"]
    assert len(group_calls) == 1
    assert _seg_type(group_calls[0][1]["message"]) == "image"
    state = await get_db().get_task_state(TASK_TOMORROW)
    assert state.status == "success" and state.business_key == tomorrow.isoformat()

    await jobs._job_tomorrow()  # 幂等
    assert len(bot.calls) == 1

    # 无班次日：纯文本播报（模拟下一期）
    fake_api.roster.assignments = []
    await get_db().set_task_state(TASK_TOMORROW, "failed", "2020-01-01", "x")
    await jobs._job_tomorrow()
    group_calls = [c for c in bot.calls if c[0] == "send_group_msg"]
    assert len(group_calls) == 2
    assert "无值班安排" in FakeBot.plain(group_calls[1][1])


async def test_group_weekly_api_error(app: App, fake_api, monkeypatch):
    """群每周预告接口异常：任务状态记失败（业务键 + 错误摘要），不向群发送。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.api import DutyApiError
    from nonebot_plugin_duty_bot.storage import TASK_GROUP_WEEKLY, get_db
    from nonebot_plugin_duty_bot.timeutil import this_week_range

    async def boom(student_no=None):
        raise DutyApiError("接口返回 500")

    monkeypatch.setattr(fake_api, "get_roster", boom)
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_group_weekly()
    monday, _ = this_week_range(_today())
    state = await get_db().get_task_state(TASK_GROUP_WEEKLY)
    assert state.status == "failed"
    assert state.business_key == monday.isoformat()
    assert "接口返回 500" in (state.error or "")
    assert len(bot.calls) == 0


async def test_tomorrow_api_error(app: App, fake_api, monkeypatch):
    """明日播报接口异常：任务状态记失败（业务键 + 错误摘要），不向群发送。"""
    from nonebot_plugin_duty_bot import jobs
    from nonebot_plugin_duty_bot.api import DutyApiError
    from nonebot_plugin_duty_bot.storage import TASK_TOMORROW, get_db
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    async def boom(student_no=None):
        raise DutyApiError("接口返回 500")

    monkeypatch.setattr(fake_api, "get_roster", boom)
    bot = FakeBot()
    monkeypatch.setattr(jobs, "try_get_bot", lambda: bot)

    await jobs._job_tomorrow()
    tomorrow = now_beijing().date() + timedelta(days=1)
    state = await get_db().get_task_state(TASK_TOMORROW)
    assert state.status == "failed"
    assert state.business_key == tomorrow.isoformat()
    assert "接口返回 500" in (state.error or "")
    assert len(bot.calls) == 0


async def test_jobs_registered(app: App):
    """定时任务已按北京时间注册；个人每周预告下次执行在周日。"""
    import nonebot_plugin_apscheduler
    from nonebot_plugin_duty_bot import jobs

    scheduler = nonebot_plugin_apscheduler.scheduler
    for job_id in jobs.JOB_TASK_MAP:
        job = scheduler.get_job(job_id)
        assert job is not None, job_id

    job = scheduler.get_job(jobs.JOB_PERSONAL_WEEKLY)
    assert job.next_run_time is not None
    assert job.next_run_time.weekday() == 6  # 周日（Python: 周一=0）

    job = scheduler.get_job(jobs.JOB_GROUP_WEEKLY)
    assert job.next_run_time.weekday() == 0  # 周一

    job = scheduler.get_job(jobs.JOB_TOMORROW)
    assert job.next_run_time.hour == 21


@pytest.mark.parametrize(
    "field",
    [
        "dutybot_personal_weekly_cron",
        "dutybot_group_weekly_cron",
        "dutybot_tomorrow_cron",
    ],
)
def test_config_rejects_invalid_cron(field: str):
    """非法 cron 在配置构造时即抛 ValidationError，消息含字段名（fail fast）。"""
    import pydantic

    from nonebot_plugin_duty_bot.config import Config

    with pytest.raises(pydantic.ValidationError) as excinfo:
        Config(**{field: "bad"})
    assert field in str(excinfo.value)


def test_config_cron_defaults_valid():
    """三个 cron 默认值均通过预校验（conftest 未注入 cron，走默认值）。"""
    from nonebot_plugin_duty_bot.config import Config

    config = Config()  # validate_default=True：默认值也经 from_crontab 校验
    assert config.dutybot_personal_weekly_cron == "0 19 * * sun"
    assert config.dutybot_group_weekly_cron == "0 8 * * mon"
    assert config.dutybot_tomorrow_cron == "0 21 * * *"


async def test_startup_config_warning(app: App, monkeypatch):
    """启动检查：API/群号未配置记录警告，配置齐全静默；均不抛异常。"""
    import loguru

    from nonebot_plugin_duty_bot import check_startup_config
    from nonebot_plugin_duty_bot.config import plugin_config

    records: list[str] = []
    sink_id = loguru.logger.add(
        lambda message: records.append(str(message)), level="WARNING", format="{message}"
    )
    try:
        monkeypatch.setattr(plugin_config, "dutybot_api_base_url", "")
        monkeypatch.setattr(plugin_config, "dutybot_api_key", "")
        monkeypatch.setattr(plugin_config, "dutybot_group_id", 0)
        check_startup_config()
        text = "\n".join(records)
        assert "值班系统 API 未配置" in text
        assert "值班群号未配置" in text

        records.clear()
        monkeypatch.setattr(plugin_config, "dutybot_api_base_url", "http://127.0.0.1:8787")
        monkeypatch.setattr(plugin_config, "dutybot_api_key", "key")
        monkeypatch.setattr(plugin_config, "dutybot_group_id", 10086)
        check_startup_config()
        assert records == []
    finally:
        loguru.logger.remove(sink_id)


def _seg_type(message) -> str:
    """取消息首个片段类型（jobs 直接发送 MessageSegment，命令回复才是 Message）。"""
    from nonebot.adapters.onebot.v11 import Message

    if isinstance(message, Message):
        return message[0].type
    return message.type


# ---------- 工具 ----------


def _today():
    from nonebot_plugin_duty_bot.timeutil import now_beijing

    return now_beijing().date()


async def _bind_user(qq: int, student_no: str = "2025123456789", name: str = "张三"):
    from nonebot_plugin_duty_bot.storage import get_db

    await get_db().bind(qq, student_no, name)
