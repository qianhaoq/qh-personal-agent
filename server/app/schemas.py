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


def _check_datetime_representable(value: datetime | None) -> datetime | None:
    """时间字段必须能安全换算成 UTC（4c1c9be P2-4）：aware 的 astimezone
    不能越界（9999-12-31-05:00 → 10000 年会 OverflowError → 500）；naive
    的按最坏 IANA 偏移（±14h）预留边界。"""
    if value is None:
        return value
    if value.tzinfo is not None:
        try:
            value.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise ValueError("带时区的时间换算成 UTC 后超出可表示范围") from exc
    elif not _NAIVE_DT_MIN <= value <= _NAIVE_DT_MAX:
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
        raise ValueError(f"timezone 必须是合法 IANA 时区名：{value}") from exc
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
