"""请求/响应模型与序列化。"""
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Note, Reminder
from app.timeutil import to_iso


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
    text: str = Field(min_length=1, max_length=20000)
    recorded_at: datetime | None = None
    timezone: str | None = None
    source: str = Field(default="app", max_length=16)
    asr: dict | None = None


class NotePatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=20000)
    rev: int


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_time: datetime | None = None
    timezone: str | None = None
