"""请求/响应模型与序列化。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.models import Note, Reminder
from app.timeutil import to_iso

# asr 元数据序列化后的体积上限
_MAX_ASR_JSON_BYTES = 4 * 1024
# 笔记正文长度上限（字符）
_MAX_NOTE_TEXT_CHARS = 5000


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
    if len(value) > 64:
        raise ValueError("timezone 长度需 <= 64")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone 必须是合法 IANA 时区名：{value}") from exc
    return value


# ---- 请求体 ----


class LoginRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=64)
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="android", max_length=32)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class NoteCreate(BaseModel):
    client_note_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=_MAX_NOTE_TEXT_CHARS)
    recorded_at: datetime | None = None
    timezone: str | None = None
    source: str = Field(default="app", max_length=16)
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
        if len(json.dumps(value, ensure_ascii=False)) > _MAX_ASR_JSON_BYTES:
            raise ValueError(f"asr 序列化后超过 {_MAX_ASR_JSON_BYTES} 字节")
        return value


class NotePatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=_MAX_NOTE_TEXT_CHARS)
    rev: int


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_time: datetime | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value):
        return _check_timezone(value)
