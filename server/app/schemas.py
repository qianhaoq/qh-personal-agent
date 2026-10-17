"""请求/响应模型与序列化。"""
import json
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.models import Note, Reminder
from app.timeutil import to_iso

# asr 元数据序列化后的体积上限
_MAX_ASR_JSON_BYTES = 4 * 1024
# 笔记正文长度上限（字符）
_MAX_NOTE_TEXT_CHARS = 5000


def reject_nul(value: str) -> str:
    """字符串入参一律拒绝 NUL（\\x00）。

    NUL 会被 PostgreSQL 文本列直接拒绝（asyncpg 编码错误 → 500），也会
    破坏下游的 JSON 序列化与 LLM prompt；在 schemas 层统一拦成 422。
    """
    if "\x00" in value:
        raise ValueError("字符串不允许包含 NUL 字符（\\x00）")
    return value


NoNulStr = Annotated[str, AfterValidator(reject_nul)]


def _reject_nul_everywhere(obj: object) -> None:
    """递归拒绝容器（dict/list）里任何层级字符串中的 NUL（asr 元数据用）。"""
    if isinstance(obj, str):
        reject_nul(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _reject_nul_everywhere(key)
            _reject_nul_everywhere(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _reject_nul_everywhere(item)


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
    recorded_at: datetime | None = None
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
        # 容器内任何层级的字符串（键/值）都不允许 NUL
        _reject_nul_everywhere(value)
        if len(json.dumps(value, ensure_ascii=False)) > _MAX_ASR_JSON_BYTES:
            raise ValueError(f"asr 序列化后超过 {_MAX_ASR_JSON_BYTES} 字节")
        return value


class NotePatch(BaseModel):
    text: NoNulStr | None = Field(default=None, min_length=1, max_length=_MAX_NOTE_TEXT_CHARS)
    rev: int


class AskRequest(BaseModel):
    text: NoNulStr = Field(min_length=1, max_length=2000)
    client_time: datetime | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value):
        return _check_timezone(value)
