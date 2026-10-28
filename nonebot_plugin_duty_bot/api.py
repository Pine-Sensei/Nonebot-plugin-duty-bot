"""值班系统 Bot API 客户端。

仅使用已发布排班查询接口：GET /api/bot/roster 与 GET /api/bot/today。
- Bearer 认证（BOT_API_KEY）
- 统一处理错误响应、限流（429 遵循 Retry-After）与超时
- 整体查询（不带 student_no）支持短时缓存
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from typing import Any, Literal, Optional

import httpx
from nonebot import logger
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import plugin_config

# 契约要求的时间格式：两位小时:两位分钟（见 openapi-bot.yaml）
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
# 契约要求的日期格式：YYYY-MM-DD（见 openapi-bot.yaml format: date）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: Optional[str]) -> Optional[str]:
    """校验契约日期格式；null（历史班次日期未定）放行。"""
    if value is not None:
        if not _DATE_RE.fullmatch(value):
            raise ValueError(f"invalid date: {value}")
        try:
            date.fromisoformat(value)
        except ValueError as e:
            raise ValueError(f"invalid date: {value}") from e
    return value


class Teammate(BaseModel):
    name: str
    role: Literal["primary", "backup"]


class Assignment(BaseModel):
    """学生的一条排班分配记录。"""

    id: str
    slot_id: str
    student_no: str
    name: str
    role: Literal["primary", "backup"]
    version: int
    active: int
    cycle_id: str
    cycle_name: str
    week_no: int
    weekday: int
    date: Optional[str] = None
    start_time: str
    end_time: str
    post_name: str
    teammates: list[Teammate] = Field(default_factory=list)

    @field_validator("start_time", "end_time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """契约要求 HH:MM 两位数字时间格式。"""
        if not _TIME_RE.fullmatch(value):
            raise ValueError(f"invalid time: {value}")
        return value

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: Optional[str]) -> Optional[str]:
        """契约要求 YYYY-MM-DD 日期格式（可为 null）。"""
        return _validate_date(value)


class Slot(BaseModel):
    """班次（时间-岗位）记录。"""

    slot_id: str
    cycle_id: str
    cycle_name: str
    week_no: int
    weekday: int
    date: Optional[str] = None
    start_time: str
    end_time: str
    post_name: str
    primary_need: int
    backup_need: int

    @field_validator("start_time", "end_time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """契约要求 HH:MM 两位数字时间格式。"""
        if not _TIME_RE.fullmatch(value):
            raise ValueError(f"invalid time: {value}")
        return value

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: Optional[str]) -> Optional[str]:
        """契约要求 YYYY-MM-DD 日期格式（可为 null）。"""
        return _validate_date(value)


class Roster(BaseModel):
    """排班查询结果。"""

    date: str
    timezone: str = "Asia/Shanghai"
    assignments: list[Assignment] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)


class DutyApiError(Exception):
    """值班系统接口错误（已归一化，可直接向用户展示或记日志）。"""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class DutyApiRateLimited(DutyApiError):
    """触发接口限流，附建议等待秒数。"""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message, status=429)
        self.retry_after = max(retry_after, 1)


class DutyApiClient:
    """值班系统 Bot API 异步客户端。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 10.0,
        cache_seconds: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._cache_seconds = max(cache_seconds, 0)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Roster]] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """API 地址与密钥是否已配置。"""
        return bool(self._base_url) and bool(self._api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        """发起 GET 请求并解析 Envelope，失败时抛 DutyApiError。"""
        client = self._get_client()
        try:
            response = await client.get(path, params=params)
        except httpx.TimeoutException as e:
            raise DutyApiError(f"接口超时: {e.__class__.__name__}") from e
        except httpx.HTTPError as e:
            raise DutyApiError(f"网络错误: {e.__class__.__name__}") from e

        if response.status_code == 429:
            retry_after = 1
            if (header := response.headers.get("Retry-After")) and header.isdigit():
                retry_after = int(header)
            raise DutyApiRateLimited("接口限流，请稍后重试", retry_after)

        try:
            payload = response.json()
        except ValueError as e:
            raise DutyApiError(f"响应解析失败(HTTP {response.status_code})") from e

        # 契约规定顶层为 Envelope 对象；合法 JSON 但非对象（字符串/数组等）同样归一化
        if not isinstance(payload, dict):
            raise DutyApiError(f"响应格式异常(HTTP {response.status_code})")

        if response.status_code != 200 or payload.get("code") != 0:
            message = str(payload.get("message") or f"HTTP {response.status_code}")
            raise DutyApiError(message, status=response.status_code)

        return payload.get("data")

    async def _query(self, path: str, student_no: str | None = None) -> Roster:
        params = {"student_no": student_no} if student_no else None
        data = await self._get_json(path, params=params)
        try:
            roster = Roster.model_validate(data)
        except ValidationError as e:
            # 响应与契约不符：详情仅记日志，对外归一化为 DutyApiError
            logger.warning(f"值班系统接口响应格式异常: {e}")
            raise DutyApiError("接口响应格式异常") from e

        # 仅整体查询（无 student_no）进入短时缓存
        if student_no is None and self._cache_seconds > 0:
            self._cache[path] = (time.monotonic(), roster)
        return roster

    async def _query_cached(self, path: str) -> Roster:
        """整体查询：命中缓存直接返回；未命中时持锁回源，并发请求只放行一个。"""
        if self._cache_seconds > 0 and (entry := self._cache.get(path)):
            fetched_at, roster = entry
            if time.monotonic() - fetched_at < self._cache_seconds:
                return roster
        async with self._cache_lock:
            # 双重检查：等待锁期间可能已有并发请求写入缓存
            if self._cache_seconds > 0 and (entry := self._cache.get(path)):
                fetched_at, roster = entry
                if time.monotonic() - fetched_at < self._cache_seconds:
                    return roster
            return await self._query(path)

    async def get_roster(self, student_no: str | None = None) -> Roster:
        """查询已发布排班；传 student_no 时仅返回该学生的分配。"""
        if not self.configured:
            raise DutyApiError("值班系统接口未配置")
        student_no = student_no or None  # 空字符串归一为整体查询
        if student_no is None:
            return await self._query_cached("/api/bot/roster")
        return await self._query("/api/bot/roster", student_no)

    async def get_today(self, student_no: str | None = None) -> Roster:
        """查询北京时间今日排班；传 student_no 时仅返回该学生的分配。"""
        if not self.configured:
            raise DutyApiError("值班系统接口未配置")
        student_no = student_no or None  # 空字符串归一为整体查询
        if student_no is None:
            return await self._query_cached("/api/bot/today")
        return await self._query("/api/bot/today", student_no)

    async def probe(self) -> tuple[bool, str]:
        """接口可用性检查（供「状态」命令使用）。"""
        if not self.configured:
            return False, "接口未配置"
        try:
            await self._get_json("/api/bot/today")
        except DutyApiRateLimited:
            return True, "正常（已触发限流）"
        except DutyApiError as e:
            logger.warning(f"值班系统接口探测失败: {e.message}")
            return False, e.message
        return True, "正常"


# 全局客户端实例，随宿主启动/关闭管理（见 __init__.py）
client: DutyApiClient | None = None


def get_client() -> DutyApiClient:
    """获取全局 API 客户端。"""
    global client
    if client is None:
        client = DutyApiClient(
            base_url=plugin_config.dutybot_api_base_url,
            api_key=plugin_config.dutybot_api_key,
            timeout=plugin_config.dutybot_api_timeout,
            cache_seconds=plugin_config.dutybot_roster_cache_seconds,
        )
    return client


def reset_client() -> None:
    """重置全局客户端（测试与关闭时使用）。"""
    global client
    client = None


async def close_client() -> None:
    """关闭全局客户端的 HTTP 连接。"""
    global client
    if client is not None:
        await client.close()
    client = None
