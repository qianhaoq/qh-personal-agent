"""提醒确认、取消、完成与贪睡：POST /v1/reminders/{id}/{confirm,cancel,complete,snooze} — Subscription 直通原 WorkItem。"""
import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.common import new_id
from app.db import get_sessionmaker
from app.deps import get_current_user_id
from app.domain.engine import get_engine
from app.domain.model import (
    TRIGGER_USER_CANCELLED_REMINDER,
    TRIGGER_USER_COMPLETED_REMINDER,
    TRIGGER_USER_CONFIRMED_REMINDER,
    TRIGGER_USER_SNOOZED_REMINDER,
    Trigger,
)
from app.errors import ApiError
from app.models import Reminder, SaChannelAction
from app.schemas import NoNulStr, SafeDatetime, action_to_dict, reminder_to_dict
from app.timeutil import to_iso, utcnow

router = APIRouter(prefix="/v1/reminders", tags=["reminders"])


class ReminderConfirmRequest(BaseModel):
    """Optional modifications applied atomically with confirm."""

    rev: int = Field(ge=1, le=2_147_483_647)
    fire_at: SafeDatetime | None = None
    title: NoNulStr | None = Field(default=None, max_length=256)


@router.post("/{reminder_id}/confirm")
async def confirm_reminder(
    reminder_id: Annotated[NoNulStr, Path()],
    body: ReminderConfirmRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Confirm a suggested reminder (or re-confirm / modify a scheduled one).

    Contract:
    - Auth: Bearer JWT (device-bound), same as other /v1 routes.
    - Body: `{ "rev": <current rev>, "fire_at": <optional iso8601>, "title": <optional str> }`.
    - Effect: Trigger `user_confirmed_reminder` fans out via active Subscription
      on `note:{note_id}` to the original organize WorkItem — **no Policy Eval,
      no new Ticket**. Channel emits a sync-visible `schedule_reminder` action.
    - observe Policy: organize never emits schedule/alarm; only this confirm path does.
    - Idempotent: current rev + unchanged values returns 200 with the existing action.
    """
    maker = get_sessionmaker()
    async with maker() as session:
        reminder = (await session.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )).scalar_one_or_none()
        if reminder is None or reminder.deleted_at is not None:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.note_id is None:
            raise ApiError(409, "conflict", "reminder has no note thread")
        if reminder.status not in ("suggested", "scheduled"):
            raise ApiError(409, "conflict", "reminder not confirmable")
        note_id = reminder.note_id

    payload: dict = {"reminder_id": reminder_id, "note_id": note_id, "rev": body.rev}
    if body.fire_at is not None:
        payload["fire_at"] = to_iso(body.fire_at)
    if body.title is not None:
        payload["title"] = body.title

    # Stable JSON distinguishes an omitted override from an explicit empty title.
    # Hash only overrides; revision scope allows A→B→A without unbounded keys.
    mods = json.dumps(
        {"title": body.title, "fire_at": payload.get("fire_at")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    digest = hashlib.sha256(mods.encode("utf-8")).hexdigest()[:16]
    dedupe_key = f"reminder_confirm:{reminder_id}:r{body.rev}:{digest}"

    trigger = Trigger(
        id=new_id("trg"),
        kind=TRIGGER_USER_CONFIRMED_REMINDER,
        source="http_api",
        dedupe_key=dedupe_key,
        user_id=user_id,
        payload=payload,
        occurred_at=utcnow(),
        thread_key=f"note:{note_id}",
    )
    receipt = await get_engine().receive(trigger)
    if receipt.decision is None or receipt.decision.outcome != "accept":
        reason = (receipt.decision.reason if receipt.decision else "ignored")
        raise ApiError(409, "conflict", reason)

    async with maker() as read:
        fresh = await read.get(Reminder, reminder_id)
        action_row = (await read.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"schedule_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
    if fresh is None:
        raise ApiError(404, "not_found", "reminder not found")
    # Duplicate intake skips Channel, so enforce INV-11 on the fresh revision.
    if receipt.duplicate and fresh.rev != body.rev:
        raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
    return {
        "reminder": reminder_to_dict(fresh),
        "action": action_to_dict(action_row) if action_row else None,
        "work_item_id": receipt.work_item.id if receipt.work_item else None,
        "duplicate": fresh.rev == body.rev,
    }


class ReminderCancelRequest(BaseModel):
    rev: int = Field(ge=1, le=2_147_483_647)


@router.post("/{reminder_id}/cancel")
async def cancel_reminder(
    reminder_id: Annotated[NoNulStr, Path()],
    body: ReminderCancelRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Cancel suggested/scheduled reminders via the original Subscription Ticket."""
    maker = get_sessionmaker()
    async with maker() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.note_id is None:
            raise ApiError(409, "conflict", "reminder has no note thread")
        note_id = reminder.note_id
    receipt = await get_engine().receive(Trigger(
        id=new_id("trg"), kind=TRIGGER_USER_CANCELLED_REMINDER, source="http_api",
        dedupe_key=f"reminder_cancel:{reminder_id}:r{body.rev}", user_id=user_id,
        payload={"reminder_id": reminder_id, "note_id": note_id, "rev": body.rev},
        occurred_at=utcnow(), thread_key=f"note:{note_id}",
    ))
    if receipt.decision is None or receipt.decision.outcome != "accept":
        raise ApiError(409, "conflict", "提醒当前不可取消")
    async with maker() as read:
        fresh = await read.get(Reminder, reminder_id)
        action = (await read.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"cancel_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
    if fresh is None or fresh.deleted_at is not None or fresh.user_id != user_id:
        raise ApiError(404, "not_found", "reminder not found")
    # Duplicate intake bypasses Channel; current revision must still match.
    if receipt.duplicate and fresh.rev != body.rev:
        raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
    return {
        "reminder": reminder_to_dict(fresh),
        "action": action_to_dict(action) if action else None,
        "work_item_id": receipt.work_item.id if receipt.work_item else None,
        "duplicate": fresh.rev == body.rev,
    }


class ReminderCompleteRequest(BaseModel):
    rev: int = Field(ge=1, le=2_147_483_647)


@router.post("/{reminder_id}/complete")
async def complete_reminder(
    reminder_id: Annotated[NoNulStr, Path()],
    body: ReminderCompleteRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Complete suggested/scheduled reminders via the original Subscription Ticket."""
    maker = get_sessionmaker()
    async with maker() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.note_id is None:
            raise ApiError(409, "conflict", "reminder has no note thread")
        note_id = reminder.note_id
    receipt = await get_engine().receive(Trigger(
        id=new_id("trg"), kind=TRIGGER_USER_COMPLETED_REMINDER, source="http_api",
        dedupe_key=f"reminder_complete:{reminder_id}:r{body.rev}", user_id=user_id,
        payload={"reminder_id": reminder_id, "note_id": note_id, "rev": body.rev},
        occurred_at=utcnow(), thread_key=f"note:{note_id}",
    ))
    if receipt.decision is None or receipt.decision.outcome != "accept":
        raise ApiError(409, "conflict", "提醒当前不可完成")
    async with maker() as read:
        fresh = await read.get(Reminder, reminder_id)
        action = (await read.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"complete_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
    if fresh is None or fresh.deleted_at is not None or fresh.user_id != user_id:
        raise ApiError(404, "not_found", "reminder not found")
    # Duplicate intake bypasses Channel; current revision must still match.
    if receipt.duplicate and fresh.rev != body.rev:
        raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
    return {
        "reminder": reminder_to_dict(fresh),
        "action": action_to_dict(action) if action else None,
        "work_item_id": receipt.work_item.id if receipt.work_item else None,
        "duplicate": fresh.rev == body.rev,
    }


class ReminderSnoozeRequest(BaseModel):
    rev: int = Field(ge=1, le=2_147_483_647)
    minutes: int = Field(default=10, ge=1, le=1440)


@router.post("/{reminder_id}/snooze")
async def snooze_reminder(
    reminder_id: Annotated[NoNulStr, Path()],
    body: ReminderSnoozeRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Snooze suggested/scheduled reminders via the original Subscription Ticket."""
    maker = get_sessionmaker()
    async with maker() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.note_id is None:
            raise ApiError(409, "conflict", "reminder has no note thread")
        note_id = reminder.note_id
    receipt = await get_engine().receive(Trigger(
        id=new_id("trg"), kind=TRIGGER_USER_SNOOZED_REMINDER, source="http_api",
        dedupe_key=f"reminder_snooze:{reminder_id}:r{body.rev}:m{body.minutes}", user_id=user_id,
        payload={"reminder_id": reminder_id, "note_id": note_id, "rev": body.rev, "minutes": body.minutes},
        occurred_at=utcnow(), thread_key=f"note:{note_id}",
    ))
    if not receipt.duplicate and (receipt.decision is None or receipt.decision.outcome != "accept"):
        raise ApiError(409, "conflict", "提醒当前不可贪睡")
    async with maker() as read:
        fresh = await read.get(Reminder, reminder_id)
        action = (await read.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"schedule_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
    if fresh is None or fresh.deleted_at is not None or fresh.user_id != user_id:
        raise ApiError(404, "not_found", "reminder not found")
    # Intake replay is successful even when its revision is now stale; never snooze twice.
    return {
        "reminder": reminder_to_dict(fresh),
        "action": action_to_dict(action) if action else None,
        "work_item_id": receipt.work_item.id if receipt.work_item else None,
        "duplicate": receipt.duplicate,
    }
