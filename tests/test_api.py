"""API 客户端单元测试（基于 httpx.MockTransport，无真实网络）。"""

import asyncio

import httpx
import pytest


def _client_mod():
    from nonebot_plugin_duty_bot import api

    return api


def _envelope(data) -> dict:
    return {"code": 0, "message": "ok", "data": data}


ROSTER_DATA = {
    "date": "2026-10-27",
    "timezone": "Asia/Shanghai",
    "assignments": [
        {
            "id": "a-1",
            "slot_id": "s-1",
            "student_no": "2025123456789",
            "name": "张三",
            "role": "primary",
            "version": 0,
            "active": 1,
            "cycle_id": "c-1",
            "cycle_name": "第一周期",
            "week_no": 5,
            "weekday": 1,
            "date": "2026-10-27",
            "start_time": "08:00",
            "end_time": "10:00",
            "post_name": "东门岗",
            "teammates": [{"name": "李四", "role": "backup"}],
        }
    ],
    "slots": [
        {
            "slot_id": "s-1",
            "cycle_id": "c-1",
            "cycle_name": "第一周期",
            "week_no": 5,
            "weekday": 1,
            "date": "2026-10-27",
            "start_time": "08:00",
            "end_time": "10:00",
            "post_name": "东门岗",
            "primary_need": 1,
            "backup_need": 1,
        }
    ],
}


def make_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_not_configured() -> None:
    api = _client_mod()
    client = api.DutyApiClient(base_url="", api_key="")
    assert not client.configured
    with pytest.raises(api.DutyApiError):
        await client.get_roster()


async def test_get_roster_success() -> None:
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer key-1"
        assert request.url.params.get("student_no") == "123"
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient("http://test", "key-1", transport=make_transport(handler))
    roster = await client.get_roster("123")
    assert isinstance(roster, api.Roster)
    assert roster.assignments[0].name == "张三"
    await client.close()


async def test_error_envelope() -> None:
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"code": 401, "message": "unauthorized", "data": None}
        )

    client = api.DutyApiClient("http://test", "bad", transport=make_transport(handler))
    with pytest.raises(api.DutyApiError) as e:
        await client.get_roster()
    assert e.value.status == 401
    await client.close()


async def test_rate_limited() -> None:
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"code": 429, "message": "rate limited", "data": {"retryAfterSeconds": 7}},
            headers={"Retry-After": "7"},
        )

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiRateLimited) as e:
        await client.get_roster()
    assert e.value.retry_after == 7
    await client.close()


async def test_timeout() -> None:
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiError) as e:
        await client.get_roster()
    assert "超时" in e.value.message
    await client.close()


async def test_whole_query_cached() -> None:
    """整体查询命中短时缓存；按学号查询不缓存。"""
    api = _client_mod()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient(
        "http://test", "k", cache_seconds=60, transport=make_transport(handler)
    )
    await client.get_roster()
    await client.get_roster()  # 命中缓存
    assert calls["n"] == 1
    await client.get_roster("123")  # 个人查询不缓存
    assert calls["n"] == 2
    await client.get_roster()  # 仍命中整体缓存
    assert calls["n"] == 2
    await client.close()


async def test_cache_expired() -> None:
    """TTL 过期后整体查询重新回源。"""
    api = _client_mod()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient(
        "http://test", "k", cache_seconds=60, transport=make_transport(handler)
    )
    await client.get_roster()
    assert calls["n"] == 1
    # 将已写入的缓存时间戳回拨到 TTL 之前，模拟缓存过期
    fetched_at, cached = client._cache["/api/bot/roster"]
    client._cache["/api/bot/roster"] = (fetched_at - 61, cached)
    await client.get_roster()  # 过期后重新请求
    assert calls["n"] == 2
    await client.close()


async def test_rate_limited_without_header() -> None:
    """429 无 Retry-After header 时使用缺省等待秒数。"""
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"code": 429, "message": "rate limited", "data": None}
        )

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiRateLimited) as e:
        await client.get_roster()
    assert e.value.retry_after == 1
    await client.close()


async def test_rate_limited_invalid_header() -> None:
    """429 Retry-After 非数字时使用缺省等待秒数。"""
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"code": 429, "message": "rate limited", "data": None},
            headers={"Retry-After": "soon"},
        )

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiRateLimited) as e:
        await client.get_roster()
    assert e.value.retry_after == 1
    await client.close()


async def test_non_json_response() -> None:
    """响应体非 JSON 时归一化为 DutyApiError。"""
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiError) as e:
        await client.get_roster()
    assert "解析失败" in e.value.message
    await client.close()


async def test_non_zero_code() -> None:
    """HTTP 200 但 code≠0 时抛出含服务端 message 的 DutyApiError。"""
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 1001, "message": "业务错误", "data": None}
        )

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiError) as e:
        await client.get_roster()
    assert e.value.message == "业务错误"
    assert e.value.status == 200
    await client.close()


async def test_get_today() -> None:
    """get_today 不带/带学号时均请求 /api/bot/today。"""
    api = _client_mod()
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.params.get("student_no")))
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    roster = await client.get_today()
    assert seen == [("/api/bot/today", None)]
    assert roster.date == "2026-10-27"
    await client.get_today("123")
    assert seen[-1] == ("/api/bot/today", "123")
    await client.close()


async def test_malformed_data() -> None:
    """data 缺少必填字段时归一化为 DutyApiError，不外溢 ValidationError。"""
    api = _client_mod()
    bad_data = {"date": "2026-10-27", "assignments": [{"id": "a-1"}], "slots": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(bad_data))

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    with pytest.raises(api.DutyApiError) as e:
        await client.get_roster()
    assert e.value.message == "接口响应格式异常"
    await client.close()


async def test_non_dict_json_payload() -> None:
    """响应为合法 JSON 但非对象（字符串/数组/数字）时归一化为 DutyApiError，不外溢 AttributeError。"""
    api = _client_mod()

    for payload in ("unauthorized", [1, 2], 200):

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
        with pytest.raises(api.DutyApiError) as e:
            await client.get_roster()
        assert "格式异常" in e.value.message
        # probe 同样只感知 DutyApiError：/状态 命令不应因非对象载荷崩溃
        ok, message = await client.probe()
        assert not ok and "格式异常" in message
        await client.close()


async def test_invalid_date_format() -> None:
    """assignment/slot 的 date 不满足契约 YYYY-MM-DD 时归一化为 DutyApiError。"""
    api = _client_mod()
    base_assignment = ROSTER_DATA["assignments"][0]
    base_slot = ROSTER_DATA["slots"][0]
    bad_data_variants = [
        {"assignments": [{**base_assignment, "date": bad_date}]}
        for bad_date in ("2026/10/27", "2026-10-45", "2026-13-01")
    ] + [
        {"slots": [{**base_slot, "date": "2026/10/27"}]},
    ]

    for variant in bad_data_variants:

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_envelope({**ROSTER_DATA, **variant})
            )

        client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
        with pytest.raises(api.DutyApiError) as e:
            await client.get_roster()
        assert e.value.message == "接口响应格式异常"
        await client.close()


async def test_invalid_time_format() -> None:
    """start_time 不满足契约 HH:MM 格式时归一化为 DutyApiError。"""
    api = _client_mod()
    base_assignment = ROSTER_DATA["assignments"][0]

    for bad_time in ("8:00", "08:00:00"):

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_envelope(
                    {**ROSTER_DATA, "assignments": [{**base_assignment, "start_time": bad_time}]}
                ),
            )

        client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
        with pytest.raises(api.DutyApiError) as e:
            await client.get_roster()
        assert e.value.message == "接口响应格式异常"
        await client.close()


async def test_invalid_role() -> None:
    """role 不在契约枚举（primary/backup）内时拒绝。"""
    api = _client_mod()
    base_assignment = ROSTER_DATA["assignments"][0]
    bad_assignments = [
        {**base_assignment, "role": "xxx"},  # Assignment.role 非法
        {**base_assignment, "teammates": [{"name": "王五", "role": "xxx"}]},  # Teammate.role 非法
    ]

    for bad_assignment in bad_assignments:

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_envelope({**ROSTER_DATA, "assignments": [bad_assignment]}),
            )

        client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
        with pytest.raises(api.DutyApiError):
            await client.get_roster()
        await client.close()


async def test_empty_student_no_normalized() -> None:
    """空字符串学号归一为整体查询：不发参数且命中整体缓存。"""
    api = _client_mod()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.params.get("student_no") is None  # 不携带学号参数
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient(
        "http://test", "k", cache_seconds=60, transport=make_transport(handler)
    )
    await client.get_roster("")
    await client.get_roster("")  # 第二次命中整体查询缓存
    assert calls["n"] == 1
    await client.close()


async def test_concurrent_single_flight() -> None:
    """并发整体查询未命中缓存时只发起一次 HTTP 请求。"""
    api = _client_mod()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.05)  # 模拟慢响应，让并发请求都进入锁等待
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient(
        "http://test", "k", cache_seconds=60, transport=make_transport(handler)
    )
    first, second = await asyncio.gather(client.get_roster(), client.get_roster())
    assert calls["n"] == 1
    assert first is second  # 后到者命中刚写入的缓存，拿到同一实例
    await client.close()


async def test_probe() -> None:
    api = _client_mod()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(ROSTER_DATA))

    client = api.DutyApiClient("http://test", "k", transport=make_transport(handler))
    assert await client.probe() == (True, "正常")
    await client.close()

    async def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "message": "boom", "data": None})

    client2 = api.DutyApiClient("http://test", "k", transport=make_transport(fail_handler))
    ok, message = await client2.probe()
    assert not ok and "boom" in message
    await client2.close()
