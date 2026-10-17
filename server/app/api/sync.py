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
    """返回 cursor 之后变化的 notes/reminders 与被删除的 id。cursor 为全局单调序列。

    notes 与 reminders（含软删墓碑）合并为按 change_seq 升序的单一流分页：
    两张表各取 limit+1 条，归并后统一截断到 limit，多出的那条只用来判断
    has_more。若按表各自截断，一张表的热更新会把另一张表的变更挤出本页，
    客户端按 cursor 续拉时会漏数据。
    """
    notes = (
        await session.execute(
            select(Note)
            .where(Note.user_id == user_id, Note.change_seq > since)
            .order_by(Note.change_seq)
            .limit(limit + 1)
        )
    ).scalars().all()
    reminders = (
        await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.change_seq > since)
            .order_by(Reminder.change_seq)
            .limit(limit + 1)
        )
    ).scalars().all()

    # 合并流：(change_seq, 表序) 排序；表序只用于同 seq 时保持稳定顺序
    merged = sorted(
        [(n.change_seq, 0, n) for n in notes] + [(r.change_seq, 1, r) for r in reminders],
        key=lambda t: (t[0], t[1]),
    )
    has_more = len(merged) > limit
    page = merged[:limit]

    deleted_notes = [row.id for _, tag, row in page if tag == 0 and row.deleted_at is not None]
    deleted_reminders = [row.id for _, tag, row in page if tag == 1 and row.deleted_at is not None]
    live_notes = [note_to_dict(row) for _, tag, row in page if tag == 0 and row.deleted_at is None]
    live_reminders = [
        reminder_to_dict(row) for _, tag, row in page if tag == 1 and row.deleted_at is None
    ]

    return {
        "cursor": page[-1][0] if page else since,
        "notes": live_notes,
        "reminders": live_reminders,
        "deleted": {"notes": deleted_notes, "reminders": deleted_reminders},
        "has_more": has_more,
    }
