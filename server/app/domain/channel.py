"""Unified result outlet. Invoked only by Engine inside its fenced transaction.

Workers never push notifications or set system alarms. Client-visible side
effects are Channel actions (sync-visible payloads) plus note/reminder rows.
"""
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.common import allocate_seq, new_id
from app.domain.model import (
    CHANNEL_ACTION_CANCEL_REMINDER,
    CHANNEL_ACTION_COMPLETE_REMINDER,
    CHANNEL_ACTION_SCHEDULE_REMINDER,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_REMINDER_CONFIRM,
)
from app.errors import ApiError
from app.models import Note, Reminder, SaChannelAction, SaSubscription
from app.schemas import clean_llm_str
from app.timeutil import as_utc, to_iso, to_iso_z, utcnow


class Channel(Protocol):
    async def publish(self, session, item, output) -> None: ...
    async def failed(self, session, item) -> None: ...
    def result(self, item) -> dict: ...
    async def confirm_reminder(self, session, *, work_item_id: str, user_id: str,
                               payload: dict) -> dict | None: ...
    async def cancel_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None: ...

    async def complete_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None: ...

    async def snooze_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None: ...


class SyncChannel:
    async def publish(self, session, item, output) -> None:
        if item.kind == "organize_note" and output is not None:
            from app.workers.extract_impl import apply_output
            snapshot, extraction = output
            # Observe default: suggested reminders only — never schedule/alarm actions.
            # Subscription upsert happens inside apply_output before allocate_seq (INV-1).
            await apply_output(
                session, item.note_id, item.note_rev, snapshot, extraction, work_item=item,
            )
        # answer_ask JSON result is persisted by Engine and returned by result().

    async def failed(self, session, item) -> None:
        if item.kind != "organize_note":
            return
        changed = await session.execute(update(Note).where(
            Note.id == item.note_id, Note.rev == item.note_rev,
            Note.deleted_at.is_(None), Note.status != "organized",
        ).values(status="failed", updated_at=utcnow()))
        if changed.rowcount != 1:
            return
        await session.flush()
        seq = await allocate_seq(session)
        await session.execute(update(Note).where(Note.id == item.note_id).values(change_seq=seq))

    def result(self, item) -> dict:
        if item.status != "done" or item.result is None:
            raise RuntimeError("result unavailable")
        return item.result

    async def confirm_reminder(self, session, *, work_item_id: str, user_id: str,
                               payload: dict) -> dict | None:
        """suggested→scheduled + emit schedule_reminder action (client payload).

        Lock order: reminders → sa_channel_actions INSERT → sequences (INV-1).
        Idempotent on dedupe_key = schedule_reminder:{reminder_id}.
        """
        reminder_id = payload.get("reminder_id")
        if not isinstance(reminder_id, str) or not reminder_id:
            return None

        stmt = select(Reminder).where(Reminder.id == reminder_id)
        if session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        reminder = (await session.execute(stmt)).scalar_one_or_none()
        if reminder is None or reminder.deleted_at is not None:
            return None
        if reminder.user_id != user_id:
            return None
        if reminder.rev != payload.get("rev"):
            raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
        if reminder.status not in ("suggested", "scheduled"):
            return None

        title_override = payload.get("title")
        fire_at_override = payload.get("fire_at")
        if isinstance(fire_at_override, str):
            fire_at_override = datetime.fromisoformat(fire_at_override)
        now = utcnow()
        new_title = (
            clean_llm_str(title_override, limit=256)
            if title_override is not None else reminder.title
        )
        new_fire = fire_at_override if fire_at_override is not None else reminder.fire_at

        new_fire = as_utc(new_fire)
        # Product choice: reject expired effective times, including empty overrides.
        # Read the clock after acquiring the reminder lock (INV-13).
        if new_fire is None or new_fire <= now:
            raise ApiError(409, "conflict", "reminder time has expired")

        if reminder.status == "suggested":
            changed = await session.execute(update(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.rev == payload["rev"],
                Reminder.status == "suggested",
                Reminder.deleted_at.is_(None),
                Reminder.user_id == user_id,
            ).values(
                status="scheduled",
                title=new_title,
                fire_at=new_fire,
                rev=Reminder.rev + 1,
                updated_at=now,
            ))
            if changed.rowcount != 1:
                # PG holds the row lock; SQLite's conditional write also fences rev.
                await session.refresh(reminder)
                raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
            await session.refresh(reminder)
        else:
            # Already scheduled: apply modifications when provided.
            if (
                (title_override is not None or fire_at_override is not None)
                and (new_title != reminder.title or new_fire != as_utc(reminder.fire_at))
            ):
                changed = await session.execute(update(Reminder).where(
                    Reminder.id == reminder_id,
                    Reminder.rev == payload["rev"],
                    Reminder.status == "scheduled",
                    Reminder.deleted_at.is_(None),
                ).values(
                    title=new_title,
                    fire_at=new_fire,
                    rev=Reminder.rev + 1,
                    updated_at=now,
                ))
                if changed.rowcount != 1:
                    await session.refresh(reminder)
                    raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
                await session.refresh(reminder)

        dedupe_key = f"schedule_reminder:{reminder_id}"
        existing = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == dedupe_key))).one_or_none()

        action_payload = {
            "reminder_id": reminder.id,
            "note_id": reminder.note_id,
            "title": reminder.title,
            "fire_at": to_iso_z(reminder.fire_at),
            "work_item_id": work_item_id,
            "rev": reminder.rev,
        }

        if existing is not None:
            # Idempotent re-confirm: refresh payload if reminder mutated; reuse seq
            # when unchanged so sync does not re-emit. Modifications bump seq.
            same = existing.payload == action_payload and existing.deleted_at is None
            if same:
                return _action_dict(existing)
            existing.payload = action_payload
            existing.work_item_id = work_item_id
            existing.deleted_at = None
            await session.flush()
            total = 2  # reminder row + action row
            last_seq = await allocate_seq(session, count=total)
            seqs = iter(range(last_seq - total + 1, last_seq + 1))
            reminder.change_seq = next(seqs)
            existing.change_seq = next(seqs)
            await session.flush()
            return _action_dict(existing)

        action = SaChannelAction(
            id=new_id("act"),
            user_id=user_id,
            type=CHANNEL_ACTION_SCHEDULE_REMINDER,
            payload=action_payload,
            work_item_id=work_item_id,
            reminder_id=reminder.id,
            dedupe_key=dedupe_key,
            change_seq=0,
            created_at=now,
        )
        session.add(action)
        await session.flush()
        total = 2
        last_seq = await allocate_seq(session, count=total)
        seqs = iter(range(last_seq - total + 1, last_seq + 1))
        reminder.change_seq = next(seqs)
        action.change_seq = next(seqs)
        await session.flush()
        return _action_dict(action)

    async def snooze_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None:
        """Reschedule under reminder lock; actions precede sequences (INV-1/11/13)."""
        reminder_id = payload.get("reminder_id")
        stmt = select(Reminder).where(Reminder.id == reminder_id)
        if session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        reminder = (await session.execute(stmt)).scalar_one_or_none()
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.rev != payload.get("rev"):
            raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
        if reminder.status not in ("suggested", "scheduled"):
            raise ApiError(409, "conflict", "提醒当前不可贪睡")
        now = utcnow()
        changed = await session.execute(update(Reminder).where(
            Reminder.id == reminder_id, Reminder.user_id == user_id,
            Reminder.deleted_at.is_(None), Reminder.rev == payload["rev"],
            Reminder.status.in_(("suggested", "scheduled")),
        ).values(status="scheduled", fire_at=now + timedelta(minutes=payload["minutes"]),
                 rev=Reminder.rev + 1, updated_at=now))
        if changed.rowcount != 1:
            await session.refresh(reminder)
            if reminder.deleted_at is not None or reminder.user_id != user_id:
                raise ApiError(404, "not_found", "reminder not found")
            raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
        await session.refresh(reminder)

        dedupe_key = f"schedule_reminder:{reminder_id}"
        existing = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == dedupe_key))).one_or_none()

        action_payload = {
            "reminder_id": reminder.id,
            "note_id": reminder.note_id,
            "title": reminder.title,
            "fire_at": to_iso_z(reminder.fire_at),
            "work_item_id": work_item_id,
            "rev": reminder.rev,
        }

        if existing is not None:
            existing.payload = action_payload
            existing.work_item_id = work_item_id
            existing.deleted_at = None
            await session.flush()
            total = 2  # reminder row + action row
            last_seq = await allocate_seq(session, count=total)
            seqs = iter(range(last_seq - total + 1, last_seq + 1))
            reminder.change_seq = next(seqs)
            existing.change_seq = next(seqs)
            await session.flush()
            return _action_dict(existing)

        action = SaChannelAction(
            id=new_id("act"),
            user_id=user_id,
            type=CHANNEL_ACTION_SCHEDULE_REMINDER,
            payload=action_payload,
            work_item_id=work_item_id,
            reminder_id=reminder.id,
            dedupe_key=dedupe_key,
            change_seq=0,
            created_at=now,
        )
        session.add(action)
        await session.flush()
        total = 2
        last_seq = await allocate_seq(session, count=total)
        seqs = iter(range(last_seq - total + 1, last_seq + 1))
        reminder.change_seq = next(seqs)
        action.change_seq = next(seqs)
        await session.flush()
        return _action_dict(action)

    async def cancel_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None:
        """Cancel under the reminder lock; all action writes precede sequences."""
        reminder_id = payload.get("reminder_id")
        stmt = select(Reminder).where(Reminder.id == reminder_id)
        if session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        reminder = (await session.execute(stmt)).scalar_one_or_none()
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.rev != payload.get("rev"):
            raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
        dedupe_key = f"cancel_reminder:{reminder_id}"
        if reminder.status == "cancelled":
            existing = (await session.scalars(select(SaChannelAction).where(
                SaChannelAction.dedupe_key == dedupe_key,
                SaChannelAction.deleted_at.is_(None),
            ))).one_or_none()
            return _action_dict(existing) if existing else None
        if reminder.status not in ("suggested", "scheduled"):
            return None

        now = utcnow()  # INV-13: acquired reminder lock before reading clock.
        changed = await session.execute(update(Reminder).where(
            Reminder.id == reminder_id, Reminder.user_id == user_id,
            Reminder.rev == payload["rev"], Reminder.deleted_at.is_(None),
            Reminder.status.in_(("suggested", "scheduled")),
        ).values(status="cancelled", rev=Reminder.rev + 1, updated_at=now))
        if changed.rowcount != 1:
            await session.refresh(reminder)  # INV-12: bypass stale identity map.
            if reminder.deleted_at is not None or reminder.user_id != user_id:
                raise ApiError(404, "not_found", "reminder not found")
            if reminder.rev != payload["rev"]:
                raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
            return None
        await session.refresh(reminder)
        schedule = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"schedule_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
        if schedule is not None:
            schedule.deleted_at = now
        action = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == dedupe_key,
        ))).one_or_none()
        action_payload = {
            "reminder_id": reminder.id, "note_id": reminder.note_id,
            "title": reminder.title, "fire_at": to_iso_z(reminder.fire_at),
            "work_item_id": work_item_id, "rev": reminder.rev,
        }
        if action is None:
            action = SaChannelAction(
                id=new_id("act"), user_id=user_id, type=CHANNEL_ACTION_CANCEL_REMINDER,
                payload=action_payload, work_item_id=work_item_id, reminder_id=reminder.id,
                dedupe_key=dedupe_key, change_seq=0, created_at=now,
            )
            session.add(action)
        else:
            action.payload = action_payload
            action.work_item_id = work_item_id
            action.deleted_at = None
        await session.flush()
        rows = [reminder, action] + ([schedule] if schedule is not None else [])
        last_seq = await allocate_seq(session, count=len(rows))
        for row, seq in zip(rows, range(last_seq - len(rows) + 1, last_seq + 1)):
            row.change_seq = seq
        await session.flush()
        return _action_dict(action)

    async def complete_reminder(self, session, *, work_item_id: str, user_id: str,
                              payload: dict) -> dict | None:
        """Complete under the reminder lock; all action writes precede sequences."""
        reminder_id = payload.get("reminder_id")
        stmt = select(Reminder).where(Reminder.id == reminder_id)
        if session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        reminder = (await session.execute(stmt)).scalar_one_or_none()
        if reminder is None or reminder.deleted_at is not None or reminder.user_id != user_id:
            raise ApiError(404, "not_found", "reminder not found")
        if reminder.rev != payload.get("rev"):
            raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
        dedupe_key = f"complete_reminder:{reminder_id}"
        if reminder.status == "done":
            existing = (await session.scalars(select(SaChannelAction).where(
                SaChannelAction.dedupe_key == dedupe_key,
                SaChannelAction.deleted_at.is_(None),
            ))).one_or_none()
            return _action_dict(existing) if existing else None
        if reminder.status not in ("suggested", "scheduled"):
            return None

        now = utcnow()  # INV-13: acquired reminder lock before reading clock.
        changed = await session.execute(update(Reminder).where(
            Reminder.id == reminder_id, Reminder.user_id == user_id,
            Reminder.rev == payload["rev"], Reminder.deleted_at.is_(None),
            Reminder.status.in_(("suggested", "scheduled")),
        ).values(status="done", rev=Reminder.rev + 1, updated_at=now))
        if changed.rowcount != 1:
            await session.refresh(reminder)  # INV-12: bypass stale identity map.
            if reminder.deleted_at is not None or reminder.user_id != user_id:
                raise ApiError(404, "not_found", "reminder not found")
            if reminder.rev != payload["rev"]:
                raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")
            return None
        await session.refresh(reminder)
        schedule = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == f"schedule_reminder:{reminder_id}",
            SaChannelAction.deleted_at.is_(None),
        ))).one_or_none()
        if schedule is not None:
            schedule.deleted_at = now
        action = (await session.scalars(select(SaChannelAction).where(
            SaChannelAction.dedupe_key == dedupe_key,
        ))).one_or_none()
        action_payload = {
            "reminder_id": reminder.id, "note_id": reminder.note_id,
            "title": reminder.title, "fire_at": to_iso_z(reminder.fire_at),
            "work_item_id": work_item_id, "rev": reminder.rev,
        }
        if action is None:
            action = SaChannelAction(
                id=new_id("act"), user_id=user_id, type=CHANNEL_ACTION_COMPLETE_REMINDER,
                payload=action_payload, work_item_id=work_item_id, reminder_id=reminder.id,
                dedupe_key=dedupe_key, change_seq=0, created_at=now,
            )
            session.add(action)
        else:
            action.payload = action_payload
            action.work_item_id = work_item_id
            action.deleted_at = None
        await session.flush()
        rows = [reminder, action] + ([schedule] if schedule is not None else [])
        last_seq = await allocate_seq(session, count=len(rows))
        for row, seq in zip(rows, range(last_seq - len(rows) + 1, last_seq + 1)):
            row.change_seq = seq
        await session.flush()
        return _action_dict(action)

    async def _upsert_subscription(self, session, item) -> None:
        insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
        now = utcnow()
        await session.execute(insert(SaSubscription).values(
            id=new_id("sub"),
            thread_key=item.thread_key,
            work_item_id=item.id,
            user_id=item.user_id,
            kind=SUBSCRIPTION_REMINDER_CONFIRM,
            status=SUBSCRIPTION_ACTIVE,
            created_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=["thread_key", "kind"],
            set_={
                "work_item_id": item.id,
                "user_id": item.user_id,
                "status": SUBSCRIPTION_ACTIVE,
                "updated_at": now,
            },
        ))
        await session.flush()


def _action_dict(row: SaChannelAction) -> dict:
    return {
        "id": row.id,
        "type": row.type,
        "payload": row.payload,
        "work_item_id": row.work_item_id,
        "reminder_id": row.reminder_id,
        "created_at": to_iso(row.created_at),
    }
