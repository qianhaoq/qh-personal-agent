"""MockLLM：无 API key / LLM_PROVIDER=mock 时的确定性规则实现。

- 抽取（chat 带 response_format）：识别“明天/下午三点/X分钟后”等生成 fire_at，
  “提醒/要/记得”等生成 todo，标签用简单关键词。
- 问答（chat 不带 response_format）：基于 prompt 里给出的记忆上下文作答，没有则
  返回“我没有相关记录”。
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas import utc_safe_datetime

DEFAULT_TZ = "Asia/Shanghai"

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

TAG_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("工作", ("会议", "项目", "汇报", "上班", "加班", "客户", "领导", "周报")),
    ("购物", ("买", "超市", "网购", "下单", "快递")),
    ("家庭", ("家", "爸妈", "妈妈", "爸爸", "孩子", "老婆", "老公")),
    ("健康", ("医院", "体检", "吃药", "运动", "跑步", "健身", "牙")),
    ("学习", ("学习", "看书", "课程", "考试", "复习", "英语")),
    ("朋友", ("朋友", "同学", "聚餐", "聚会", "吃饭")),
    ("旅行", ("旅行", "旅游", "机票", "酒店", "火车", "出差")),
    ("财务", ("工资", "报销", "缴费", "房租", "还款", "银行")),
]

TODO_KEYWORDS = ("提醒", "记得", "别忘", "需要", "要办", "要买", "要去", "要做", "要给", "要发", "要回", "要交", "要开", "得去", "得买")

_RE_IN_MINUTES = re.compile(r"([0-9]+|[一二两三四五六七八九十]+)分钟后")
_RE_IN_HOURS = re.compile(r"([0-9]+|[一二两三四五六七八九十]+)个?小时后")
_RE_IN_DAYS = re.compile(r"([0-9]+|[一二两三四五六七八九十]+)天后")
_RE_CLOCK = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,2})\s*点"
    r"(半|三十分|([0-9]{1,2}|[一二两三四五六七八九十]{1,3})分?)?"
)
_RE_DAY = re.compile(r"(今天|明天|后天|大后天)")
# 只有明确指向未来的日子词才默认生成 9:00 的提醒；“今天”单独出现不算时间
_FUTURE_DAY_WORDS = {"明天", "后天", "大后天"}


# 十进制数量的位数上界（INV-7，threads9 P2 / mock.py:49）：CPython 的
# int(str) 对约 4300 位以上的十进制串直接 ValueError——一条合法的 5000
# 字符笔记含「(4900+ 位数字)分钟后」会在 _safe_relative 之前就炸。6 位
# （999999 分钟 ≈ 1.9 年）以内照常解析；更大的数量本就荒谬，返回 None
# （该短语不出提醒）。有意不用正则 [0-9]{1,6} 限位：超长数字串的末 6 位
# 仍会匹配「分钟后」前的窗口，把 5000 位数错当 999999 分钟
_MAX_DIGITS = 6


def _cn_to_int(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        if len(s) > _MAX_DIGITS:
            return None
        try:
            return int(s)
        except (ValueError, OverflowError):
            # 防御：位数已限，理论上不可达；解析失败按「无时间」处理，
            # 不把异常抛上去让确定性重试耗尽、把有效笔记标 failed
            return None
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    # 十几 / 二十几 等两位组合
    if "十" in s:
        parts = s.split("十")
        left = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        right = _CN_DIGITS.get(parts[1], 0) if parts[1] else 0
        return left * 10 + right
    return None


def _day_offset(word: str) -> int:
    return {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}.get(word, 0)


def _safe_relative(base: datetime, **delta: float) -> datetime | None:
    """相对时间的安全构造（threads8 P2 / INV-7）：巨大数量（如
    「999999999999999999分钟后」「10000000天后」）会让 timedelta 构造或
    datetime 加法抛 OverflowError，或把结果推出 utc_safe_datetime 的
    ±14h 安全线——都视为「这条文本解析不出时间」，返回 None（不出提醒），
    而不是把异常抛上去让确定性重试耗尽、把有效笔记标 failed。"""
    try:
        dt = base + timedelta(**delta)
    except (OverflowError, ValueError, OSError):
        return None
    return utc_safe_datetime(dt)


def parse_datetime(text: str, now: datetime, tzname: str = DEFAULT_TZ) -> datetime | None:
    """从中文文本里解析一个相对/绝对时间。找不到返回 None。"""
    tz = ZoneInfo(tzname)
    base = now.astimezone(tz)

    def resolve(dt: datetime) -> datetime:
        """把“当天已过”的时间顺延到明天，保证 fire_at 在未来。"""
        if dt <= base:
            dt += timedelta(days=1)
        return dt

    m = _RE_IN_MINUTES.search(text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            return _safe_relative(base, minutes=n)
    m = _RE_IN_HOURS.search(text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            return _safe_relative(base, hours=n)
    m = _RE_IN_DAYS.search(text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            return _safe_relative(base, days=n)

    day_m = _RE_DAY.search(text)
    clock_m = _RE_CLOCK.search(text)
    day_off = _day_offset(day_m.group(1)) if day_m else 0

    if clock_m:
        period, hour_s, minute_part = clock_m.group(1), clock_m.group(2), clock_m.group(3)
        hour = _cn_to_int(hour_s)
        if hour is None:
            return None
        minute = 30 if minute_part in ("半", "三十分") else 0
        if minute_part and minute_part not in ("半", "三十分"):
            mm = _cn_to_int(minute_part.rstrip("分"))
            minute = mm if mm is not None else 0
        if (period in ("下午", "傍晚", "晚上") and hour < 12) or (
            period == "中午" and hour < 11
        ):
            hour += 12
        # INV-7（threads11 P2，mock.py:139）：时分越界（“99 点”“3 点 99 分”）
        # 不钳位也不上抛——min(hour, 23) 会把“99 点”变成错误的 23:00 提醒，
        # ValueError 会 500。视为没解析出时间，返回 None（无提醒）。
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_off)
        return resolve(dt) if day_off == 0 else dt

    if day_m and day_m.group(1) in _FUTURE_DAY_WORDS:
        # 只说了“明天”，默认上午 9 点
        dt = base.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=day_off)
        return dt

    return None


def _split_clauses(text: str) -> list[str]:
    parts = re.split(r"[，。,；;！!？?\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def extract_structured(text: str, now: datetime, tzname: str = DEFAULT_TZ) -> dict:
    """确定性抽取：标题/标签/待办/建议提醒。"""
    clauses = _split_clauses(text)

    title = clauses[0][:20] if clauses else "未命名笔记"

    tags = []
    for tag, words in TAG_KEYWORDS:
        if any(w in text for w in words) and tag not in tags:
            tags.append(tag)
        if len(tags) >= 5:
            break

    todos = []
    reminders = []

    def add_reminder(title_text: str) -> None:
        fire_at = parse_datetime(title_text, now, tzname)
        if fire_at is None:
            return
        if any(r["title"] == title_text for r in reminders):
            return
        reminders.append(
            {"title": title_text[:100], "fire_at": fire_at.isoformat()}
        )

    for clause in clauses:
        is_todo = any(k in clause for k in TODO_KEYWORDS) or parse_datetime(clause, now, tzname) is not None
        if is_todo and clause not in [t["text"] for t in todos]:
            todos.append({"text": clause[:200]})
        add_reminder(clause)

    return {"title": title, "tags": tags, "todos": todos, "suggested_reminders": reminders}


class MockLLM:
    """确定性 mock：同样输入永远同样输出，测试与本地开发用。"""

    async def chat(self, messages: list[dict], response_format: dict | None = None) -> str:
        if response_format is not None:
            return self._extraction(messages)
        return self._answer(messages)

    # ---- 内部 ----

    @staticmethod
    def _extract_note_and_now(messages: list[dict]) -> tuple[str, datetime, str]:
        note_text = ""
        now = datetime.now(UTC)
        tzname = DEFAULT_TZ
        for msg in messages:
            content = str(msg.get("content", ""))
            m = re.search(r"<note>(.*?)</note>", content, re.DOTALL)
            if m:
                note_text = m.group(1).strip()
            m = re.search(r"当前时间[:：]\s*([0-9T:+\-.Zz]+)", content)
            if m:
                try:
                    now = datetime.fromisoformat(m.group(1))
                except ValueError:
                    pass
            m = re.search(r"时区[:：]\s*([A-Za-z0-9_+\-/]+)", content)
            if m:
                # INV-7（threads12 P2，mock.py:222）：完整保留**已校验**的
                # 时区——UTC（无斜杠段）不再漏捕回退 Asia/Shanghai，多段
                # IANA 名（America/Argentina/Buenos_Aires）不再被窄正则
                # 截成前两段（截断值过 ZoneInfo 会抛错）。正则只圈词法
                # 合法的候选，有效性交给 ZoneInfo 判定：非法候选保持
                # 默认/已捕获值，绝不让 ZoneInfo 异常炸掉整次抽取
                try:
                    ZoneInfo(m.group(1))
                    tzname = m.group(1)
                except (ZoneInfoNotFoundError, ValueError):
                    pass
        return note_text, now, tzname

    def _extraction(self, messages: list[dict]) -> str:
        note_text, now, tzname = self._extract_note_and_now(messages)
        result = extract_structured(note_text or "空笔记", now, tzname)
        return json.dumps(result, ensure_ascii=False)

    def _answer(self, messages: list[dict]) -> str:
        context = ""
        question = ""
        for msg in messages:
            content = str(msg.get("content", ""))
            if msg.get("role") == "user":
                question = content
            m = re.search(r"<memories>(.*?)</memories>", content, re.DOTALL)
            if m:
                context = m.group(1).strip()
        if not context:
            return "我没有相关记录。"
        first_line = context.splitlines()[0].strip()
        return f"根据你的记录：{first_line}（供参考，与问题“{question[:30]}”相关）"
