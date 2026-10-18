"""MockLLM 的确定性规则与中文时间解析。"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.llm.mock import MockLLM, extract_structured, parse_datetime

TZ = "Asia/Shanghai"


def _now() -> datetime:
    # 固定基准：2026-10-16 20:00 (UTC) = 2026-10-17 04:00 (+08)
    return datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


def test_parse_x_minutes_later():
    dt = parse_datetime("30分钟后提醒我", _now())
    assert dt is not None
    assert dt.astimezone(ZoneInfo(TZ)) == datetime(2026, 10, 17, 4, 30, tzinfo=ZoneInfo(TZ))


def test_parse_chinese_number_minutes():
    dt = parse_datetime("十分钟后出发", _now())
    assert dt is not None
    assert dt.astimezone(ZoneInfo(TZ)).minute == 10


def test_parse_hours_and_days():
    dt = parse_datetime("两个小时后开会", _now())
    assert dt is not None
    assert dt.astimezone(ZoneInfo(TZ)).hour == 6
    dt2 = parse_datetime("三天后交报告", _now())
    assert dt2 is not None
    assert dt2.astimezone(ZoneInfo(TZ)).day == 20


def test_parse_tomorrow_afternoon_three():
    dt = parse_datetime("明天下午三点提醒我", _now())
    assert dt is not None
    local = dt.astimezone(ZoneInfo(TZ))
    assert (local.year, local.month, local.day, local.hour) == (2026, 10, 18, 15)


def test_parse_afternoon_three_today_or_tomorrow():
    # 基准本地 04:00，今天下午三点还没过 → 今天 15:00
    dt = parse_datetime("下午三点去银行", _now())
    local = dt.astimezone(ZoneInfo(TZ))
    assert (local.day, local.hour) == (17, 15)

    # 基准本地 16:00，今天下午三点已过 → 顺延明天
    late = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)  # 本地 16:00
    dt2 = parse_datetime("下午三点去银行", late)
    local2 = dt2.astimezone(ZoneInfo(TZ))
    assert local2.day == 18


def test_parse_half_hour():
    dt = parse_datetime("明天上午九点半站会", _now())
    local = dt.astimezone(ZoneInfo(TZ))
    assert (local.day, local.hour, local.minute) == (18, 9, 30)


def test_parse_tomorrow_only_defaults_9am():
    dt = parse_datetime("明天要交房租", _now())
    local = dt.astimezone(ZoneInfo(TZ))
    assert (local.day, local.hour) == (18, 9)


def test_parse_no_time_returns_none():
    assert parse_datetime("今天天气不错", _now()) is None


def test_extract_structured_shape():
    data = extract_structured("明天下午三点提醒我去医院体检，还要买火车票", _now())
    assert set(data) == {"title", "tags", "todos", "suggested_reminders"}
    assert data["title"].startswith("明天")
    assert "健康" in data["tags"] or "旅行" in data["tags"]
    assert any("体检" in t["text"] for t in data["todos"])
    assert any(r["fire_at"] for r in data["suggested_reminders"])
    # fire_at 可解析回 datetime 且带时区
    from datetime import datetime as dt_cls

    fire = dt_cls.fromisoformat(data["suggested_reminders"][0]["fire_at"])
    assert fire.tzinfo is not None


async def test_mockllm_chat_extraction_format():
    import json

    llm = MockLLM()
    messages = [
        {
            "role": "system",
            "content": "当前时间：2026-10-16T20:00:00+00:00\n时区：Asia/Shanghai",
        },
        {"role": "user", "content": "<note>\n明天下午三点提醒我开会\n</note>"},
    ]
    raw = await llm.chat(messages, response_format={"type": "object"})
    data = json.loads(raw)
    assert data["suggested_reminders"]
    assert data["todos"]


async def test_mockllm_chat_answer_without_context():
    llm = MockLLM()
    answer = await llm.chat([{"role": "user", "content": "问题：我喜欢吃什么"}])
    assert answer.startswith("我没有相关记录")


def test_parse_datetime_huge_relative_returns_none():
    """threads8 P2 / INV-7（mock.py:78）：巨大的相对时间数量（timedelta
    构造/加法溢出，或结果越出 utc_safe_datetime 的安全线）→ 解析为 None
    （这条文本不出提醒），不把 OverflowError 抛上去耗尽重试把笔记标
    failed。"""
    assert parse_datetime("999999999999999999分钟后提醒我喝水", _now()) is None
    assert parse_datetime("999999999999999999小时后开会", _now()) is None
    assert parse_datetime("10000000天后交报告", _now()) is None
    # 合理数量不受影响
    assert parse_datetime("30分钟后提醒我", _now()) is not None
    assert parse_datetime("三天后交报告", _now()) is not None


async def test_mockllm_huge_relative_quantity_no_reminder_no_500(client, user, run_jobs):
    """threads8 P2 端到端：正文含「999999999999999999分钟后」→ MockLLM
    不抛 OverflowError，笔记照常 organized，只是不出这条提醒。"""
    import uuid

    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Note, Reminder
    from tests.conftest import auth_headers

    created = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "999999999999999999分钟后提醒我喝水",
        },
        headers=auth_headers(user),
    )
    assert created.status_code == 202, created.text
    note_id = created.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
    assert note.status == "organized", "越界数量只影响这条提醒，不影响整理本身"
    assert note.title == "999999999999999999分钟后提醒我喝水"[:20]
    assert reminders == []


def test_parse_datetime_overlong_digit_quantity_returns_none():
    """threads9 P2 / INV-7（mock.py:49）：CPython 的 int(str) 对约 4300 位
    以上的十进制串直接 ValueError——`_RE_IN_MINUTES` 的 `[0-9]+` 无位数
    限制，5000 位数字 +「分钟后」会在 `_safe_relative` 之前就炸。位数
    上界（≤6 位）让超长数量解析为 None（不出提醒）；6 位以内照常解析。"""
    huge_minutes = "9" * 5000
    assert parse_datetime(f"{huge_minutes}分钟后提醒我喝水", _now()) is None
    assert parse_datetime(f"{'8' * 4301}天后交报告", _now()) is None
    # 7 位：超过位数上界，同样不出提醒（9999999 分钟 ≈ 19 年，荒谬数量）
    assert parse_datetime("1234567分钟后提醒我喝水", _now()) is None
    # 6 位以内仍有效
    dt = parse_datetime("999999分钟后提醒我喝水", _now())
    assert dt is not None
    assert parse_datetime("30分钟后提醒我喝水", _now()) is not None
    # 中文数字不受位数上界影响（_CN_DIGITS / 十几组合本就有界）
    assert parse_datetime("三十分钟后开会", _now()) is not None


async def test_mockllm_overlong_digit_quantity_no_reminder_no_500(client, user, run_jobs):
    """threads9 P2 端到端：一条 5000 字符内的合法笔记含 4990 位数字 +
    「天后提醒我喝水」→ MockLLM 不抛 int() 的 ValueError（4300 位上限），
    笔记照常 organized，只是这条荒谬数量不出提醒、不标 failed。"""
    import uuid

    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Note, Reminder
    from tests.conftest import auth_headers

    text = "9" * 4990 + "天后提醒我喝水"
    assert len(text) <= 5000  # 请求体 text 上限内（schemas._MAX_NOTE_TEXT_CHARS）
    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": text},
        headers=auth_headers(user),
    )
    assert created.status_code == 202, created.text
    note_id = created.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
    assert note.status == "organized", "超长数字只影响这条提醒，不影响整理本身"
    assert reminders == []
