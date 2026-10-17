"""笔记接口：幂等创建、读取、乐观锁更新、软删除。"""
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import allocate_seq, new_id
from app.config import get_settings
from app.db import get_db_session
from app.deps import get_current_user_id
from app.errors import ApiError
from app.jobs.queue import enqueue
from app.memory.base import get_memory_store
from app.models import Note, Reminder
from app.schemas import NoteCreate, NotePatch, note_to_dict
from app.timeutil import as_utc, utcnow

router = APIRouter(prefix="/v1/notes", tags=["notes"])


async def _get_owned_note(session: AsyncSession, user_id: str, note_id: str) -> Note:
    note = await session.get(Note, note_id)
    if note is None or note.user_id != user_id or note.deleted_at is not None:
        raise ApiError(404, "not_found", "笔记不存在")
    return note


@router.post("", status_code=202)
async def create_note(
    body: NoteCreate,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    existing = (
        await session.execute(
            select(Note).where(Note.user_id == user_id, Note.client_note_id == body.client_note_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # 幂等：重复提交返回已有状态
        return {
            "note_id": existing.id,
            "status": existing.status,
            "rev": existing.rev,
        }

    note = Note(
        id=new_id("nte"),
        user_id=user_id,
        client_note_id=body.client_note_id,
        text=body.text,
        source=body.source,
        recorded_at=as_utc(body.recorded_at),
        timezone=body.timezone or get_settings().default_timezone,
        status="uploaded",
        rev=1,
        change_seq=await allocate_seq(session),
        asr_meta_json=_asr_json(body.asr),
    )
    session.add(note)
    try:
        await session.flush()
        await enqueue(session, "organize_note", {"note_id": note.id, "rev": 1})
        await session.commit()
    except IntegrityError:
        # 并发提交相同 client_note_id：先查后插之间存在窗口，靠唯一约束兜底，
        # 回滚后返回已存在记录（幂等），而不是 500
        await session.rollback()
        existing = (
            await session.execute(
                select(Note).where(
                    Note.user_id == user_id, Note.client_note_id == body.client_note_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return {"note_id": existing.id, "status": existing.status, "rev": existing.rev}
    return {"note_id": note.id, "status": note.status, "rev": note.rev}


def _asr_json(asr: dict | None) -> str | None:
    return json.dumps(asr, ensure_ascii=False) if asr else None


@router.get("")
async def list_notes(
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: int = Query(default=0, ge=0),
):
    rows = (
        await session.execute(
            select(Note)
            .where(Note.user_id == user_id, Note.deleted_at.is_(None), Note.change_seq > cursor)
            .order_by(Note.change_seq)
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    items = list(rows[:limit])
    return {
        "items": [note_to_dict(n) for n in items],
        "next_cursor": items[-1].change_seq if has_more and items else None,
    }


@router.get("/{note_id}")
async def get_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    note = await _get_owned_note(session, user_id, note_id)
    return note_to_dict(note)


@router.patch("/{note_id}")
async def patch_note(
    note_id: str,
    body: NotePatch,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    # 先读只做存在性/归属检查；真正的乐观锁靠下面的条件更新原子完成，
    # 避免“读到 rev → 修改 → 写回”两步之间被并发请求插队
    note = await _get_owned_note(session, user_id, note_id)
    if body.text is None or body.text == note.text:
        return {"note": note_to_dict(note), "rev": note.rev}

    result = await session.execute(
        update(Note)
        .where(
            Note.id == note_id,
            Note.user_id == user_id,
            Note.rev == body.rev,
            Note.deleted_at.is_(None),
        )
        .values(
            text=body.text,
            rev=Note.rev + 1,
            status="uploaded",
            change_seq=await allocate_seq(session),
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        await session.rollback()
        # 复查区分 404（被并发删除/不存在）与 409（rev 已被并发修改）
        current = (
            await session.execute(select(Note).where(Note.id == note_id, Note.user_id == user_id))
        ).scalar_one_or_none()
        if current is None or current.deleted_at is not None:
            raise ApiError(404, "not_found", "笔记不存在")
        raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")

    # 旧的整理产物作废，重新入队（带最新 rev，整理侧据此丢弃过期任务）
    await _invalidate_outputs(session, note)
    await enqueue(session, "organize_note", {"note_id": note_id, "rev": body.rev + 1})
    await session.commit()
    # expire_on_commit=False：显式刷新让 note_to_dict 读到条件更新后的行
    await session.refresh(note)
    return {"note": note_to_dict(note), "rev": note.rev}


@router.delete("/{note_id}", status_code=204)
async def delete_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    note = await _get_owned_note(session, user_id, note_id)
    now = utcnow()
    note.deleted_at = now
    note.change_seq = await allocate_seq(session)
    note.updated_at = now
    # 关联提醒一并删除，记忆从 MemoryStore 删除
    reminders = (
        await session.execute(select(Reminder).where(Reminder.note_id == note.id))
    ).scalars().all()
    seq = await allocate_seq(session)
    for r in reminders:
        r.deleted_at = now
        r.change_seq = seq
    store = get_memory_store()
    await store.delete_by_note(user_id, note.id)
    await session.commit()


async def _invalidate_outputs(session: AsyncSession, note: Note) -> None:
    """重新整理前作废旧的建议提醒与记忆。已确认（scheduled 等）的提醒不动。"""
    reminders = (
        await session.execute(
            select(Reminder).where(
                Reminder.note_id == note.id,
                Reminder.deleted_at.is_(None),
                Reminder.status == "suggested",
            )
        )
    ).scalars().all()
    if reminders:
        seq = await allocate_seq(session)
        for r in reminders:
            r.deleted_at = utcnow()
            r.change_seq = seq
    store = get_memory_store()
    await store.delete_by_note(note.user_id, note.id)
