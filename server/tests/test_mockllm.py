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
