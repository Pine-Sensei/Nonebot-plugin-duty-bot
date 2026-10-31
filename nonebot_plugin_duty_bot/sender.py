"""统一发送队列与限流。

- 所有出站消息（命令回复、私聊提醒/预告、群播报、图片消息）统一入队，串行发送。
- 私聊与群聊分别按滑动窗口限流（每分钟条数可配置）；达限后排队等待放行，不丢弃。
- 某类型达限时跳过其在队消息，优先发送其他类型中可发送的消息，避免队头阻塞。
- 发送失败区分「明确失败」（协议端拒绝等）与「结果未知」（超时/网络异常，
  消息可能已实际送达），由调用方决定是否降级重发，避免用户收到重复内容。
- 用户命令的即时回复不被丢弃，且优先于批量播报/预告发送。
- 队列长度超过上限时丢弃最早入队的播报/预告类消息并记录日志。
- 队列不持久化，重启后清空。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from nonebot import logger
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, Message, NetworkError

# 消息类别
CAT_COMMAND = "command"  # 用户命令即时回复：不丢弃、优先发送
CAT_REMIND = "remind"  # 值班前提醒：不丢弃
CAT_BROADCAST = "broadcast"  # 群播报：可丢弃
CAT_FORECAST = "forecast"  # 每周预告：可丢弃

# 超限积压时允许丢弃的最早类别
DROPPABLE = frozenset({CAT_BROADCAST, CAT_FORECAST})

# 会话类型（决定走哪个限流窗口）
CHAT_PRIVATE = "private"
CHAT_GROUP = "group"

_SEND_API = {
    CHAT_PRIVATE: "send_private_msg",
    CHAT_GROUP: "send_group_msg",
}


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    uncertain: bool = False
    """True 表示发送结果未知（超时/网络异常）：消息可能已实际送达，不应重发以免重复"""


@dataclass
class _Item:
    bot: Any
    chat_type: str
    target_id: int
    message: Any
    category: str
    future: asyncio.Future[SendResult]


class SendQueue:
    """串行发送队列（含分类型滑动窗口限流、优先级与积压保护）。"""

    def __init__(
        self,
        private_per_min: int,
        group_per_min: int,
        max_size: int,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = {CHAT_PRIVATE: max(private_per_min, 1), CHAT_GROUP: max(group_per_min, 1)}
        self._max_size = max(max_size, 1)
        self._sleep = sleep
        self._clock = clock
        self._urgent: deque[_Item] = deque()
        self._normal: deque[_Item] = deque()
        # 各类型滑动窗口：记录最近 60s 内每次发送的时间戳
        self._windows: dict[str, deque[float]] = {CHAT_PRIVATE: deque(), CHAT_GROUP: deque()}
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动发送循环（幂等）。"""
        if self._task is None or self._task.done():
            self._wakeup = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def close(self) -> None:
        """停止发送循环；未发送与在途消息均按失败结算。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for item in self._drain_all():
            if not item.future.done():
                item.future.set_result(SendResult(False, "queue closed"))

    def _drain_all(self) -> Iterable[_Item]:
        while self._urgent or self._normal:
            if self._urgent:
                yield self._urgent.popleft()
            if self._normal:
                yield self._normal.popleft()

    @property
    def size(self) -> int:
        return len(self._urgent) + len(self._normal)

    # ---------- 入队 ----------

    def enqueue(
        self, bot: Any, chat_type: str, target_id: int, message: Any, category: str
    ) -> asyncio.Future[SendResult]:
        """入队一条消息，返回可等待发送结果的 Future。"""
        item = _Item(
            bot=bot,
            chat_type=chat_type,
            target_id=int(target_id),
            message=message,
            category=category,
            future=asyncio.get_running_loop().create_future(),
        )
        if category == CAT_COMMAND:
            self._urgent.append(item)
        else:
            self._normal.append(item)
        self._enforce_overflow()
        self._wakeup.set()
        if self._task is None or self._task.done():
            self.start()
        return item.future

    def _enforce_overflow(self) -> None:
        """积压保护：超上限时丢弃最早入队的播报/预告类消息。"""
        while self.size > self._max_size:
            index = next(
                (i for i, item in enumerate(self._normal) if item.category in DROPPABLE),
                None,
            )
            if index is None:
                logger.warning(f"发送队列积压 {self.size} 条且无可丢弃消息")
                return
            dropped = self._normal[index]
            del self._normal[index]
            if not dropped.future.done():
                dropped.future.set_result(SendResult(False, "dropped: queue overflow"))
            logger.warning(
                f"队列超限，丢弃最早播报/预告消息: {dropped.chat_type}({dropped.target_id})"
            )

    # ---------- 发送循环 ----------

    async def _run(self) -> None:
        while True:
            if not self._urgent and not self._normal:
                # 双检防止丢失唤醒：清空事件后再次确认队列仍为空
                self._wakeup.clear()
                if self._urgent or self._normal:
                    continue
                await self._wakeup.wait()
                continue
            item, wait = self._pop_sendable()
            if item is None:
                # 在队消息的类型均达限：等待最早放行时刻后重新评估，不阻塞其他协程；
                # 等待可被新入队唤醒（新类型可能立即可发），不陪睡整个窗口
                logger.info(f"发送队列各类型均达到限流上限，等待 {wait:.1f}s")
                await self._wait_or_wakeup(wait)
                continue
            self._acquire(item.chat_type)
            try:
                await self._send(item)
            except asyncio.CancelledError:
                raise
            except BaseException:
                # 已通过 Future 结算给调用方（见 _send），此处停止循环；
                # 队列未被清空，下一次入队会自动重启发送循环。
                return

    async def _wait_or_wakeup(self, wait: float) -> None:
        """限流等待：超时或新消息入队（唤醒事件置位）时提前返回。

        clear 与注册等待之间无 await，入队侧的 set() 不会丢失；
        事件在每次等待前复位，唤醒后重新评估仍不可发时不会忙循环。
        """
        self._wakeup.clear()
        wakeup_task = asyncio.ensure_future(self._wakeup.wait())
        sleep_task = asyncio.ensure_future(self._sleep(wait))
        try:
            await asyncio.wait({wakeup_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wakeup_task.cancel()
            sleep_task.cancel()
            await asyncio.gather(wakeup_task, sleep_task, return_exceptions=True)

    def _pop_sendable(self) -> tuple[_Item | None, float]:
        """按优先级（urgent → normal，各自 FIFO）取出第一条可发送的消息。

        队首类型达限时跳过其在队消息继续扫描，避免队头阻塞；
        在队消息全部不可发送时返回 (None, 距最早放行的等待秒数)。
        """
        now = self._clock()
        waits: dict[str, float] = {}
        for queue in (self._urgent, self._normal):
            for index, item in enumerate(queue):
                window = self._prune(item.chat_type, now)
                if len(window) < self._limits[item.chat_type]:
                    del queue[index]
                    return item, 0.0
                waits.setdefault(item.chat_type, window[0] + 60.0 - now)
        return None, min(waits.values())

    def _prune(self, chat_type: str, now: float) -> deque[float]:
        """清理窗口内已滑出 60s 的发送记录，返回该类型窗口。"""
        window = self._windows[chat_type]
        while window and now - window[0] >= 60.0:
            window.popleft()
        return window

    def _acquire(self, chat_type: str) -> None:
        """滑动窗口限流：记录本次发送时刻，占用该类型窗口名额。

        调用前已由 _pop_sendable 确认未达限；两者之间无 await，
        时间流逝只会滑出旧记录，不会出现扫描可发送而占用时达限的竞态。
        """
        now = self._clock()
        window = self._prune(chat_type, now)
        window.append(now)
        if len(window) == self._limits[chat_type]:
            logger.debug(f"发送限流窗口接近上限: {chat_type}")

    async def _send(self, item: _Item) -> None:
        api = _SEND_API[item.chat_type]
        key = "user_id" if item.chat_type == CHAT_PRIVATE else "group_id"
        message = Message(item.message) if isinstance(item.message, str) else item.message
        try:
            await item.bot.call_api(api, **{key: item.target_id, "message": message})
        except NetworkError as e:
            # 超时/网络异常：消息可能已实际送达，结算为「结果未知」，
            # 调用方据此跳过降级重发，避免用户同时收到图片与文字等重复内容
            error = f"NetworkError: {e.msg or e}"
            logger.warning(
                f"消息发送结果未知[{item.category}] {item.chat_type}({item.target_id}): {error}"
            )
            if not item.future.done():
                item.future.set_result(SendResult(False, error, uncertain=True))
        except ActionFailed as e:
            # 协议端明确拒绝（风控/参数错误等）：消息确定未送达，可安全降级重发
            error = f"ActionFailed: {e.info}"
            logger.warning(
                f"消息发送失败[{item.category}] {item.chat_type}({item.target_id}): {error}"
            )
            if not item.future.done():
                item.future.set_result(SendResult(False, error))
        except Exception as e:
            # 业务发送失败：结算为失败结果，发送循环继续处理后续消息
            logger.warning(
                f"消息发送失败[{item.category}] {item.chat_type}({item.target_id}): "
                f"{e.__class__.__name__}: {e}"
            )
            if not item.future.done():
                item.future.set_result(SendResult(False, f"{e.__class__.__name__}: {e}"))
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                # 队列关闭取消在途发送：与未发送消息统一按失败结算，不向调用方抛取消异常；
                # 在途消息可能已发出，标记结果未知
                if not item.future.done():
                    item.future.set_result(SendResult(False, "queue closed", uncertain=True))
                raise
            # 其他控制流异常（测试断言失败等）：交给调用方，不静默吞掉；
            # 发送循环随即停止（见 _run），下一次入队会自动重启。
            logger.error(
                f"消息发送中断[{item.category}] {item.chat_type}({item.target_id}): "
                f"{e.__class__.__name__}: {e}"
            )
            if not item.future.done():
                item.future.set_exception(e)
            raise
        else:
            if not item.future.done():
                item.future.set_result(SendResult(True))


# 全局发送队列，随宿主启动/关闭管理（见 __init__.py）
queue: SendQueue | None = None


def init_global(
    private_per_min: int, group_per_min: int, max_size: int
) -> SendQueue:
    """创建并启动全局发送队列。"""
    global queue
    q = SendQueue(private_per_min, group_per_min, max_size)
    q.start()
    queue = q
    return q


async def close_global() -> None:
    """停止全局发送队列并清除引用。"""
    global queue
    if queue is not None:
        await queue.close()
    queue = None


def get_queue() -> SendQueue:
    """获取全局发送队列。"""
    global queue
    if queue is None:
        raise RuntimeError("发送队列尚未初始化（插件启动阶段异常）")
    return queue


async def send_reply(
    bot: Bot, chat_type: str, target_id: int, message: Any
) -> SendResult:
    """命令即时回复：入队并等待实际发送结果。"""
    result = await get_queue().enqueue(
        bot, chat_type, target_id, message, CAT_COMMAND
    )
    return result


def reset_queue() -> None:
    """重置全局发送队列（测试与关闭时使用）。"""
    global queue
    queue = None
