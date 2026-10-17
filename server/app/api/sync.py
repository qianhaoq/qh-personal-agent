"""增量同步：GET /v1/sync?since=<cursor>。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.deps import get_current_user_id
from app.models import Note, Reminder
from app.schemas import note_to_dict, reminder_to_dict

router = APIRouter(prefix="/v1/sync", tags=["sync"])


@router.get("")
async def sync(
    since: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=100, ge=1, le=500),
):
    """返回 cursor 之后变化的 notes/reminders 与被删除的 id。cursor 为全局单调序列。"""
    notes = (
        await session.execute(
            select(Note)
            .where(Note.user_id == user_id, Note.change_seq > since)
            .order_by(Note.change_seq)
            .limit(limit)
        )
    ).scalars().all()
    reminders = (
        await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.change_seq > since)
            .order_by(Reminder.change_seq)
            .limit(limit)
        )
    ).scalars().all()

    deleted_notes = [n.id for n in notes if n.deleted_at is not None]
    deleted_reminders = [r.id for r in reminders if r.deleted_at is not None]
    live_notes = [note_to_dict(n) for n in notes if n.deleted_at is None]
    live_reminders = [reminder_to_dict(r) for r in reminders if r.deleted_at is None]

    max_seq = since
    for item in (*notes, *reminders):
        max_seq = max(max_seq, item.change_seq)

    return {
        "cursor": max_seq,
        "notes": live_notes,
        "reminders": live_reminders,
        "deleted": {"notes": deleted_notes, "reminders": deleted_reminders},
        "has_more": len(notes) >= limit or len(reminders) >= limit,
    }
