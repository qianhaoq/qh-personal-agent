"""请求/响应模型与序列化。"""
import json
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.models import Note, Reminder
from app.timeutil import to_iso

# asr 元数据序列化后的体积上限
_MAX_ASR_JSON_BYTES = 4 * 1024
# asr 元数据嵌套深度上限（4c1c9be P2-4：超深嵌套要么炸 json 解析器的
# 递归栈变 500，要么过校验后拖垮下游——一律 422）
_MAX_ASR_DEPTH = 16
# 笔记正文长度上限（字符）
_MAX_NOTE_TEXT_CHARS = 5000
# rev 列是 int4：超出范围的整数绑定到 SQL 会 OverflowError → 500
_MAX_INT32 = 2**31 - 1
# naive 时间的安全界：按最坏 IANA 偏移（±14h）本地化后仍落在 datetime
# 可表示范围内（year 1 / 9999 边界上的换算会 OverflowError → 500）
_NAIVE_DT_MIN = datetime(1, 1, 2)
_NAIVE_DT_MAX = datetime(9998, 12, 31, 23, 59, 59, 999999)


def reject_nul(value: str) -> str:
    """字符串入参一律拒绝 NUL（\\x00）。

    NUL 会被 PostgreSQL 文本列直接拒绝（asyncpg 编码错误 → 500），也会
    破坏下游的 JSON 序列化与 LLM prompt；在 schemas 层统一拦成 422。
    """
    if "\x00" in value:
        raise ValueError("字符串不允许包含 NUL 字符（\\x00）")
    return value


NoNulStr = Annotated[str, AfterValidator(reject_nul)]


def _reject_surrogate(value: str) -> None:
    """拒绝 lone surrogate（\\ud800-\\udfff）：编码不成 UTF-8，入库时
    UnicodeEncodeError → 500。typed str 字段 pydantic 自己就会拒
    （string_unicode），这里覆盖无类型的 asr 容器（键与值）。"""
    if any("\ud800" <= ch <= "\udfff" for ch in value):
        raise ValueError("字符串不允许包含孤立代理对字符（lone surrogate）")


def _reject_invalid_unicode_everywhere(obj: object, depth: int = 0) -> None:
    """递归拒绝容器（dict/list）里任何层级字符串中的 NUL 与 lone
    surrogate，并限制嵌套深度（asr 元数据用）。"""
    if depth > _MAX_ASR_DEPTH:
        raise ValueError(f"asr 嵌套深度超过 {_MAX_ASR_DEPTH} 层")
    if isinstance(obj, str):
        reject_nul(obj)
        _reject_surrogate(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _reject_invalid_unicode_everywhere(key, depth + 1)
            _reject_invalid_unicode_everywhere(value, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _reject_invalid_unicode_everywhere(item, depth + 1)


def utc_safe_datetime(value: datetime | None) -> datetime | None:
    """把 datetime 安全换算成 UTC（threads6 P2 / INV-7；公开函数——请求体
    校验与 LLM 输出解析共用，不允许各写一套边界）。aware 值换算、naive 值
    视为 UTC，结果必须落在 [_NAIVE_DT_MIN, _NAIVE_DT_MAX]（按最坏 IANA
    偏移 ±14h 预留）。换算溢出（year 1 / 9999 边界上的 astimezone）或
    越界返回 None：schema 侧据此 422，抽取侧据此**丢弃该条提醒**而不是
    上抛异常把笔记标 failed。"""
    if value is None:
        return None
    try:
        utc_value = (
            value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        )
    except (OverflowError, ValueError):
        return None
    if not _NAIVE_DT_MIN <= utc_value.replace(tzinfo=None) <= _NAIVE_DT_MAX:
        return None
    return utc_value


def _check_datetime_representable(value: datetime | None) -> datetime | None:
    """时间字段必须能安全换算成 UTC（4c1c9be P2-4）。threads5 P2-4 起
    aware 与 naive 用**同一条**安全界：换算/视为 UTC 后的值必须落在
    [0001-01-02, 9998-12-31 23:59:59.999999]（按最坏 IANA 偏移 ±14h
    预留）。此前 aware 只试「能转成 UTC」——`9999-12-31T23:59:59Z` 能过
    schema，却在抽取/提醒解析按用户时区 `astimezone` 时 OverflowError →
    note 被标 failed 而不是 422。threads6 P2 起界判定统一走
    utc_safe_datetime（LLM 输出侧复用，见 INV-7）。"""
    if value is None:
        return value
    if utc_safe_datetime(value) is None:
        if value.tzinfo is not None:
            raise ValueError(
                f"带时区的时间换算成 UTC 后需在 {_NAIVE_DT_MIN.year}–"
                f"{_NAIVE_DT_MAX.year} 年之间（与不带时区同一条安全界，"
                "预留最坏 ±14h 时区偏移）"
            )
        raise ValueError(
            f"不带时区的时间需在 {_NAIVE_DT_MIN.year}–{_NAIVE_DT_MAX.year} 年之间"
            "（本地化成 UTC 后须仍可表示）"
        )
    return value


SafeDatetime = Annotated[datetime, AfterValidator(_check_datetime_representable)]


def note_to_dict(note: Note) -> dict:
    return {
        "id": note.id,
        "client_note_id": note.client_note_id,
        "text": note.text,
        "title": note.title,
        "source": note.source,
        "recorded_at": to_iso(note.recorded_at),
        "timezone": note.timezone,
        "status": note.status,
        "category": note.category,
        "tags": note.tags or [],
        "todos": note.todos or [],
        "rev": note.rev,
        "created_at": to_iso(note.created_at),
        "updated_at": to_iso(note.updated_at),
    }


def reminder_to_dict(r: Reminder) -> dict:
    return {
        "id": r.id,
        "note_id": r.note_id,
        "title": r.title,
        "fire_at": to_iso(r.fire_at),
        "rrule": r.rrule,
        "status": r.status,
        "rev": r.rev,
        "created_at": to_iso(r.created_at),
        "updated_at": to_iso(r.updated_at),
    }


def _check_timezone(value: str | None) -> str | None:
    """timezone 必须是合法 IANA 时区名且长度 <= 64，否则 422。"""
    if value is None:
        return value
    reject_nul(value)
    if len(value) > 64:
        raise ValueError("timezone 长度需 <= 64")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # INV-14（threads10 P1，schemas.py:156）：固定文案，不插值 value——
        # validation_exception_handler 剥 input/ctx 后返回 msg，msg 本身
        # 不能回显用户提交的原值
        raise ValueError("timezone 必须是合法 IANA 时区名") from exc
    return value


# ---- 请求体 ----


class LoginRequest(BaseModel):
    invite_code: NoNulStr = Field(min_length=1, max_length=64)
    device_id: NoNulStr = Field(min_length=1, max_length=64)
    device_name: NoNulStr = Field(default="", max_length=128)
    platform: NoNulStr = Field(default="android", max_length=32)


class RefreshRequest(BaseModel):
    refresh_token: NoNulStr = Field(min_length=1)


class NoteCreate(BaseModel):
    client_note_id: NoNulStr = Field(min_length=1, max_length=64)
    text: NoNulStr = Field(min_length=1, max_length=_MAX_NOTE_TEXT_CHARS)
    recorded_at: SafeDatetime | None = None
    timezone: str | None = None
    source: NoNulStr = Field(default="app", max_length=16)
    asr: dict | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value):
        return _check_timezone(value)

    @field_validator("asr")
    @classmethod
    def _asr_size(cls, value):
        if value is None:
            return value
        # 容器内任何层级的字符串（键/值）都不允许 NUL / lone surrogate，
        # 且嵌套深度受限
        _reject_invalid_unicode_everywhere(value)
        if len(json.dumps(value, ensure_ascii=False)) > _MAX_ASR_JSON_BYTES:
            raise ValueError(f"asr 序列化后超过 {_MAX_ASR_JSON_BYTES} 字节")
        return value


class NotePatch(BaseModel):
    text: NoNulStr | None = Field(default=None, min_length=1, max_length=_MAX_NOTE_TEXT_CHARS)
    # int4 上界：超大整数绑定 SQL 会 OverflowError → 500（4c1c9be P2-4）
    rev: int = Field(ge=1, le=_MAX_INT32)


class AskRequest(BaseModel):
    text: NoNulStr = Field(min_length=1, max_length=2000)
    client_time: SafeDatetime | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value):
        return _check_timezone(value)
