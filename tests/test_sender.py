"""发送队列与限流单元测试（注入假时钟/假 sleep，确定性验证）。"""

import asyncio


class FakeBot:
    def __init__(
        self,
        fail_for: set[str] | None = None,
        clock=None,
        fail_with: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.times: list[float] = []  # 每次发送时刻（clock.now），验证限流等待
        self.fail_for = fail_for or set()
        self.clock = clock
        self.fail_with = fail_with  # 命中 fail_for 时抛出的异常（默认 RuntimeError）

    @staticmethod
    def text(kwargs) -> str:
        """提取消息纯文本（发送队列会将 str 强转为 Message）。"""
        msg = kwargs.get("message", "")
        return msg.extract_plain_text() if hasattr(msg, "extract_plain_text") else str(msg)

    async def call_api(self, api: str, **kwargs):
        text = self.text(kwargs)
        if any(marker in text for marker in self.fail_for):
            if self.fail_with is not None:
                raise self.fail_with
            raise RuntimeError("mock send failure")
        self.calls.append((api, kwargs))
        if self.clock is not None:
            self.times.append(self.clock.now)
        return {"message_id": len(self.calls)}


class Clock:
    """可控单调时钟：sleep 推进时间。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _make_queue(clock: Clock, private: int, group: int, max_size: int):
    from nonebot_plugin_duty_bot.sender import SendQueue

    return SendQueue(private, group, max_size, sleep=clock.sleep, clock=clock)


async def test_fifo_and_success() -> None:
    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    bot = FakeBot()
    f1 = q.enqueue(bot, "private", 1, "m1", "remind")
    f2 = q.enqueue(bot, "private", 2, "m2", "remind")
    assert (await f1).ok and (await f2).ok
    assert [c[1]["user_id"] for c in bot.calls] == [1, 2]
    await q.close()


async def test_command_priority_over_broadcast() -> None:
    """命令即时回复优先于批量播报发送。"""
    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    bot = FakeBot()
    f_broadcast = q.enqueue(bot, "group", 10, "播报内容", "broadcast")
    f_command = q.enqueue(bot, "group", 10, "命令回复", "command")
    await f_command
    await f_broadcast
    assert FakeBot.text(bot.calls[0][1]) == "命令回复"
    await q.close()


async def test_rate_limit_queues_and_waits() -> None:
    """达限后排队等待最早记录滑出窗口（约 60s），不丢弃、不计失败。"""
    clock = Clock()
    q = _make_queue(clock, 1, 10, 50)
    q.start()
    bot = FakeBot()
    f1 = q.enqueue(bot, "private", 1, "first", "remind")
    f2 = q.enqueue(bot, "private", 2, "second", "remind")
    assert (await f1).ok
    assert (await f2).ok  # 排队等待后成功发送
    assert any(abs(s - 60.0) < 1e-6 for s in clock.sleeps)
    await q.close()


async def test_group_and_private_windows_independent() -> None:
    """私聊与群聊分别限流，互不影响。"""
    clock = Clock()
    q = _make_queue(clock, 1, 1, 50)
    q.start()
    bot = FakeBot()
    f1 = q.enqueue(bot, "private", 1, "p1", "command")
    f2 = q.enqueue(bot, "group", 10, "g1", "command")
    f3 = q.enqueue(bot, "private", 1, "p2", "command")
    assert (await f1).ok and (await f2).ok and (await f3).ok
    await q.close()


async def test_overflow_drops_oldest_broadcast_only() -> None:
    """队列超上限时仅丢弃最早的播报/预告类消息；提醒与命令回复不被丢弃。"""
    clock = Clock()
    q = _make_queue(clock, 10, 10, 3)
    bot = FakeBot()
    # 同步连续入队：循环任务尚未运行，积压在入队阶段同步形成并触发溢出丢弃
    f_old_broadcast = q.enqueue(bot, "group", 10, "old-broadcast", "broadcast")
    f_old_forecast = q.enqueue(bot, "group", 10, "old-forecast", "forecast")
    f_remind = q.enqueue(bot, "private", 1, "reminder", "remind")
    f_command = q.enqueue(bot, "private", 2, "cmd-reply", "command")
    f_new_broadcast = q.enqueue(bot, "group", 10, "new-broadcast", "broadcast")

    # 上限 3：入队命令回复与新播报时，最早的播报与预告先后被丢弃
    assert not (await f_old_broadcast).ok
    assert not (await f_old_forecast).ok

    q.start()
    # 提醒、命令回复与新播报均不被丢弃；命令回复优先发送
    assert (await f_command).ok
    assert (await f_remind).ok
    assert (await f_new_broadcast).ok
    assert FakeBot.text(bot.calls[0][1]) == "cmd-reply"
    await q.close()


async def test_send_failure_resolves_future() -> None:
    """发送异常按失败结算，不抛出到调用方。"""
    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    bot = FakeBot(fail_for={"boom"})
    result = await q.enqueue(bot, "private", 1, "boom", "command")
    assert not result.ok and "mock send failure" in result.error
    assert not result.uncertain  # 未知异常视为明确失败，可安全降级重发
    await q.close()


async def test_send_timeout_resolves_uncertain() -> None:
    """超时/网络异常（NetworkError）：结算为「结果未知」，消息可能已实际送达。"""
    from nonebot.adapters.onebot.v11 import NetworkError

    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    bot = FakeBot(
        fail_for={"timeout"},
        fail_with=NetworkError("WebSocket call api send_private_msg timeout"),
    )
    result = await q.enqueue(bot, "private", 1, "timeout", "forecast")
    assert not result.ok and result.uncertain
    assert "timeout" in (result.error or "")
    await q.close()


async def test_send_action_failed_is_certain_failure() -> None:
    """协议端明确拒绝（ActionFailed）：确定未送达，不标记结果未知，可安全降级重发。"""
    from nonebot.adapters.onebot.v11 import ActionFailed

    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    bot = FakeBot(
        fail_for={"reject"}, fail_with=ActionFailed(retcode=1200, status="failed")
    )
    result = await q.enqueue(bot, "private", 1, "reject", "forecast")
    assert not result.ok and not result.uncertain
    assert "ActionFailed" in (result.error or "") and "1200" in (result.error or "")
    await q.close()


async def test_close_resolves_pending_as_failed() -> None:
    """关闭队列时未发送消息按失败结算（队列不持久化）。"""
    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    bot = FakeBot()
    pending = q.enqueue(bot, "private", 1, "m", "remind")  # 循环未启动
    await q.close()
    result = await pending
    assert not result.ok and "queue closed" in (result.error or "")


async def test_sliding_window_no_burst_at_boundary() -> None:
    """滑动窗口防突发：发满限速后 59s 再入队仍需等待，60s 滑出后方可发送。"""
    clock = Clock()
    q = _make_queue(clock, 2, 10, 50)
    q.start()
    bot = FakeBot()
    for i in range(2):  # t0 发满私聊限速
        assert (await q.enqueue(bot, "private", i, f"m{i}", "remind")).ok
    assert clock.sleeps == []  # 窗口内未满，直接放行
    clock.now = 59.0  # t0+59s：t0 的记录尚未滑出，不可发送
    f3 = q.enqueue(bot, "private", 3, "m3", "remind")
    assert (await f3).ok  # 等待至 t0+60s 记录滑出后放行，不丢弃
    assert abs(clock.sleeps[-1] - 1.0) < 1e-6  # 等待时长 = 60 - 59
    assert abs(clock.now - 60.0) < 1e-6
    f4 = q.enqueue(bot, "private", 4, "m4", "remind")
    assert (await f4).ok  # t0+60s 后窗口已滑空，可立即发送
    assert len(clock.sleeps) == 1  # 未产生额外等待
    assert len(bot.calls) == 4


async def test_rate_windows_independent_no_cross_reset() -> None:
    """私聊/群聊窗口完全独立：私聊放行不重置群聊计数，各自等各自窗口。"""
    clock = Clock()
    q = _make_queue(clock, 2, 2, 50)
    q.start()
    bot = FakeBot(clock=clock)
    for i in range(2):  # t=0 私聊发满
        assert (await q.enqueue(bot, "private", i, f"p{i}", "remind")).ok
    clock.now = 30.0
    for i in range(2):  # t=30 群聊发满（独立窗口，不受私聊影响）
        assert (await q.enqueue(bot, "group", i, f"g{i}", "remind")).ok
    assert abs(clock.now - 30.0) < 1e-6
    clock.now = 59.0
    f_p3 = q.enqueue(bot, "private", 3, "p3", "remind")
    f_g3 = q.enqueue(bot, "group", 3, "g3", "remind")
    assert (await f_p3).ok
    assert (await f_g3).ok
    # 私聊在 t=60 记录滑出后放行；群聊仍需等 t=30 记录滑出（t=90），未被私聊放行重置
    assert [FakeBot.text(c[1]) for c in bot.calls] == ["p0", "p1", "g0", "g1", "p3", "g3"]
    assert bot.times == [0.0, 0.0, 30.0, 30.0, 60.0, 90.0]
    assert [round(s, 6) for s in clock.sleeps] == [1.0, 30.0]


async def test_cross_type_not_blocked_by_rate_limit() -> None:
    """队首类型达限不阻塞其他类型：群聊消息与紧急命令回复立即可发。"""
    clock = Clock()
    q = _make_queue(clock, 1, 10, 50)
    q.start()
    bot = FakeBot(clock=clock)
    assert (await q.enqueue(bot, "private", 1, "p1", "remind")).ok  # 私聊达限
    f_p2 = q.enqueue(bot, "private", 1, "p2", "remind")  # 队首但类型不可发送
    f_g = q.enqueue(bot, "group", 10, "群播报", "broadcast")
    f_cmd = q.enqueue(bot, "group", 20, "命令回复", "command")
    assert (await f_cmd).ok  # 紧急命令回复不被私聊窗口卡住
    assert (await f_g).ok  # 群聊消息立即可发
    assert (await f_p2).ok  # 私聊消息等窗口放行后照常发送，不丢弃
    # 命令回复与群播报均在 t=0 零等待发出；仅 p2 等待 60s 窗口放行
    assert [FakeBot.text(c[1]) for c in bot.calls] == ["p1", "命令回复", "群播报", "p2"]
    assert bot.times == [0.0, 0.0, 0.0, 60.0]
    assert [round(s, 6) for s in clock.sleeps] == [60.0]


async def test_urgent_reply_priority_during_rate_wait() -> None:
    """限流等待期间入队的紧急回复优先于更早排队的普通群消息发送。"""
    clock = Clock()
    q = _make_queue(clock, 1, 10, 50)
    q.start()
    bot = FakeBot(clock=clock)
    assert (await q.enqueue(bot, "private", 1, "p1", "remind")).ok  # 私聊达限
    f_p2 = q.enqueue(bot, "private", 1, "p2", "remind")  # 队首类型达限，等待中
    f_normal = q.enqueue(bot, "group", 10, "普通播报", "broadcast")  # 先入队
    f_urgent = q.enqueue(bot, "group", 10, "紧急回复", "command")  # 后入队
    results = await asyncio.gather(f_urgent, f_normal, f_p2)
    assert all(r.ok for r in results)
    # p1 先发；紧急回复插队于普通群消息之前，限流中的私聊最后等窗口放行
    assert [FakeBot.text(c[1]) for c in bot.calls] == ["p1", "紧急回复", "普通播报", "p2"]
    assert bot.times == [0.0, 0.0, 0.0, 60.0]
    assert abs(clock.sleeps[-1] - 60.0) < 1e-6


async def test_rate_wait_interrupted_by_new_enqueue() -> None:
    """在队类型均达限而等待时，新入队的其他类型消息立即放行，不陪睡整个窗口。

    回归防护：等待曾用裸 sleep 实现，新入队的唤醒事件在睡眠期间被忽略。
    使用真实 sleep/clock（注入的假 sleep 立即返回，无法观察中断）。
    """
    from nonebot_plugin_duty_bot.sender import SendQueue

    q = SendQueue(1, 1, 50)  # 默认真实 asyncio.sleep + time.monotonic
    q.start()
    bot = FakeBot()
    assert (await q.enqueue(bot, "private", 1, "p1", "remind")).ok  # 私聊达限
    f_p2 = q.enqueue(bot, "private", 1, "p2", "remind")  # 私聊达限 → 循环进入约 60s 等待
    await asyncio.sleep(0.05)  # 确保循环已进入限流等待

    f_group = q.enqueue(bot, "group", 10, "群命令回复", "command")  # 群窗口有余量
    result = await asyncio.wait_for(f_group, timeout=5)  # 修复前会卡到私聊窗口放行（≈60s）
    assert result.ok
    await q.close()
    sent = [FakeBot.text(c[1]) for c in bot.calls]
    assert sent == ["p1", "群命令回复"]  # p2 尚在等待，随关闭按失败结算
    assert not (await f_p2).ok


async def test_close_settles_inflight_as_queue_closed() -> None:
    """关闭队列时在途消息按 SendResult(False, "queue closed") 结算，而非 CancelledError。"""
    clock = Clock()
    q = _make_queue(clock, 10, 10, 50)
    q.start()
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowBot(FakeBot):
        async def call_api(self, api: str, **kwargs):
            entered.set()
            await release.wait()  # 挂起在途发送，等待队列关闭时被取消
            return await super().call_api(api, **kwargs)

    bot = SlowBot()
    fut = q.enqueue(bot, "private", 1, "在途消息", "command")
    await entered.wait()  # 确保消息已进入发送阶段（call_api 挂起中）
    await q.close()
    result = await fut
    assert not result.ok and result.error == "queue closed"
