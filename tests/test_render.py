"""渲染模块单元测试：视图构建、聚合、转义、降级文本与发送降级。"""

import asyncio
from datetime import date, timedelta

from .conftest import make_assignment


class FakeQueue:
    """假发送队列：立即成功结算，记录全部发送内容。"""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def enqueue(self, bot, chat_type, target_id, message, category):
        from nonebot_plugin_duty_bot.sender import SendResult

        self.sent.append((chat_type, target_id, message, category))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(SendResult(True))
        return fut


class FailImageQueue(FakeQueue):
    """图片消息发送失败、文本消息成功的假队列。"""

    def __init__(self) -> None:
        super().__init__()
        self.image_failed = False

    def enqueue(self, bot, chat_type, target_id, message, category):
        if not isinstance(message, str):
            self.image_failed = True
            from nonebot_plugin_duty_bot.sender import SendResult

            fut = asyncio.get_running_loop().create_future()
            fut.set_result(SendResult(False, "send failed"))
            return fut
        return super().enqueue(bot, chat_type, target_id, message, category)


def _render_mod():
    from nonebot_plugin_duty_bot import render

    return render


def test_personal_view_sorted_with_undated_last() -> None:
    render = _render_mod()
    a_today = make_assignment(date="2026-10-28", start_time="08:00")
    a_late = make_assignment(id="a-2", date="2026-10-28", start_time="14:00")
    a_history = make_assignment(id="a-3", date=None)
    view = render.build_personal_view("我的排班", "张三（123）", [a_late, a_history, a_today])
    assert [r.date_display for r in view.rows] == [
        "10月28日（周三）",
        "10月28日（周三）",
        "日期未定（历史班次）",
    ]
    assert view.rows[0].time_range == "08:00-10:00"
    assert view.rows[2].role == "主值"


def test_group_day_aggregation() -> None:
    """同班次多主值顿号连接、缺省“—”、按开始时间排序。"""
    render = _render_mod()
    assignments = [
        make_assignment(id="1", slot_id="s2", start_time="14:00", end_time="16:00", post_name="西门岗", name="李四", role="primary"),
        make_assignment(id="2", slot_id="s2", start_time="14:00", end_time="16:00", post_name="西门岗", name="王五", role="backup"),
        make_assignment(id="3", slot_id="s1", start_time="08:00", end_time="10:00", post_name="东门岗", name="张三", role="primary"),
    ]
    view = render.build_group_day_view("今日值班安排", "10月27日（周二）", assignments)
    assert [r.time_range for r in view.rows] == ["08:00-10:00", "14:00-16:00"]
    assert view.rows[1].primary == "李四"
    assert view.rows[1].backup == "王五"
    assert view.rows[0].backup == "—"


def test_group_week_view_builds_week_grid() -> None:
    """周网格：固定 7 列（周一~周日），行=去重升序时段，格子=当日班次聚合；日期未定不参与。"""
    render = _render_mod()
    monday = date(2026, 11, 2)
    assignments = [
        make_assignment(id="1", date="2026-11-02", slot_id="s1"),
        make_assignment(
            id="2", date="2026-11-02", slot_id="s2",
            start_time="14:00", end_time="16:00", name="李四", role="backup",
        ),
        make_assignment(
            id="3", date="2026-11-03", slot_id="s2",
            start_time="14:00", end_time="16:00", post_name="西门岗", name="王五", role="primary",
        ),
        make_assignment(id="4", date=None, slot_id="s3"),  # 日期未定不参与
    ]
    view = render.build_group_week_view("下周值班安排", monday, assignments)
    assert [h.weekday for h in view.headers] == [
        "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
    ]
    assert view.headers[0].date_display == "11-02"
    assert view.headers[6].date_display == "11-08"
    assert view.date_range == "11月2日 - 11月8日"
    assert view.slot_times == ["08:00-10:00", "14:00-16:00"]
    assert view.cells[0][0][0].post_name == "东门岗"  # 周一早班
    assert view.cells[1][0][0].primary == "—"  # 周一午班缺主班
    assert view.cells[1][0][0].backup == "李四"
    assert view.cells[1][1][0].primary == "王五"  # 周二午班
    assert not view.cells[0][1]  # 周二无早班

    chunks = render.group_week_text_chunks(view)
    assert "【下周值班安排】11月2日 - 11月8日" in chunks[0]
    assert "—— 星期一 11-02 ——" in chunks[0]
    assert "主班: 王五" in chunks[0]
    assert "备班: 李四" in chunks[0]


def test_filter_by_dates_and_student() -> None:
    from nonebot_plugin_duty_bot.api import Roster

    render = _render_mod()
    roster = Roster(
        date="2026-10-27",
        assignments=[
            make_assignment(id="1", date="2026-10-27"),
            make_assignment(id="2", date="2026-10-30"),
            make_assignment(id="3", date=None),
            make_assignment(id="4", date="2026-10-28", student_no="999", name="他人", slot_id="sx"),
        ]
    )
    monday = date(2026, 10, 26)
    got = render.filter_by_dates(roster, monday, monday + timedelta(days=6))
    assert [a.id for a in got] == ["1", "2", "4"]
    got_mine = render.filter_by_dates(roster, monday, monday + timedelta(days=4), "2025123456789")
    assert [a.id for a in got_mine] == ["1", "2"]


def test_text_chunks() -> None:
    render = _render_mod()
    view = render.build_group_day_view("今日值班安排", "10月27日（周二）", [make_assignment()])
    chunks = render.group_day_text_chunks(view)
    assert len(chunks) == 1
    assert "【今日值班安排】10月27日（周二）" in chunks[0]
    assert "主值: 张三" in chunks[0]

    pview = render.build_personal_view("我的排班", "张三（123）", [make_assignment()])
    ptext = render.personal_text_chunks(pview)[0]
    assert "【我的排班】" in ptext and "东门岗" in ptext


def test_html_escapes_user_data() -> None:
    """姓名、岗位等数据经 HTML 转义后拼入模板，防止破坏排版结构。"""
    render = _render_mod()
    view = render.build_personal_view(
        "我的排班", '<script>alert("xss")</script>', [make_assignment(post_name="1<img>&")]
    )
    html = render._env.get_template("personal.html.j2").render(view=view)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html  # 副标题（含姓名）转义
    assert "&lt;img&gt;" in html  # 岗位名转义


async def test_render_template_disables_external_resources(monkeypatch) -> None:
    """渲染调用 htmlkit 时必须传入禁网 fetcher。"""
    render = _render_mod()
    captured = {}

    async def fake_html_to_pic(html: str, **kwargs) -> bytes:
        captured.update(kwargs)
        return b"PNG-BYTES"

    monkeypatch.setattr(render, "html_to_pic", fake_html_to_pic)
    view = render.build_personal_view("我的排班", "张三", [make_assignment()])
    data = await render.render_template("personal.html.j2", view)
    assert data == b"PNG-BYTES"
    assert captured["img_fetch_fn"] is render._deny_fetch
    assert captured["css_fetch_fn"] is render._deny_fetch


async def test_render_template_scales_with_config(monkeypatch) -> None:
    """根字号与画布宽度随 dutybot_render_scale 等比放大。"""
    render = _render_mod()
    captured = {}

    async def fake_html_to_pic(html: str, **kwargs) -> bytes:
        captured["html"] = html
        captured.update(kwargs)
        return b"PNG-BYTES"

    monkeypatch.setattr(render, "html_to_pic", fake_html_to_pic)
    view = render.build_personal_view("我的排班", "张三", [make_assignment()])

    monkeypatch.setattr(render.plugin_config, "dutybot_render_scale", 2.0)
    await render.render_template("personal.html.j2", view)
    assert captured["max_width"] == 1600
    assert "font-size: 32.0px" in captured["html"]

    monkeypatch.setattr(render.plugin_config, "dutybot_render_scale", 1.0)
    await render.render_template("personal.html.j2", view)
    assert captured["max_width"] == 800
    assert "font-size: 16.0px" in captured["html"]


async def test_deliver_view_falls_back_to_text_on_render_error(monkeypatch) -> None:
    """图片渲染异常 → 纯文本分段发送，不向用户暴露内部错误。"""
    render = _render_mod()

    async def render_fail(*args, **kwargs) -> bytes:
        raise RuntimeError("render boom")

    q = FakeQueue()
    monkeypatch.setattr(render, "get_queue", lambda: q)
    monkeypatch.setattr(render, "render_template", render_fail)
    view = render.build_personal_view("我的排班", "张三", [make_assignment()])
    result = await render.deliver_view(
        object(), "private", 1,
        template_name="personal.html.j2", view=view,
        text_chunks=render.personal_text_chunks,
    )
    assert result.ok
    assert all(s[0] == "private" and s[1] == 1 for s in q.sent)
    assert all(isinstance(s[2], str) for s in q.sent)  # 全为纯文本


async def test_deliver_view_falls_back_to_text_on_send_failure(monkeypatch) -> None:
    """图片发送失败 → 纯文本分段发送。"""
    render = _render_mod()

    async def render_ok(*args, **kwargs) -> bytes:
        return b"IMG"

    q = FailImageQueue()
    monkeypatch.setattr(render, "get_queue", lambda: q)
    monkeypatch.setattr(render, "render_template", render_ok)
    view = render.build_personal_view("我的排班", "张三", [make_assignment()])
    result = await render.deliver_view(
        object(), "group", 100,
        template_name="personal.html.j2", view=view,
        text_chunks=render.personal_text_chunks,
    )
    assert result.ok
    assert q.image_failed  # 图片发送曾失败
    assert all(isinstance(s[2], str) for s in q.sent)  # 降级文本已发送
