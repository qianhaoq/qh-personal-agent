"""笔记接口：幂等创建、读取、乐观锁更新、软删除。

加锁顺序（防 PG 死锁，规则见 app/common.py 的 allocate_seq docstring）：
所有写路径都是先锁业务行（note 的条件 UPDATE → reminders 的条件
UPDATE），最后才 allocate_seq，再用第二条 UPDATE 写 change_seq。
MemoryStore 的读写（mem0 内部还会调 LLM/embedding）一律放在数据库
事务提交之后，失败只记日志：删除路径有幂等的 cleanup_memory job 兜底，
重整理路径由 _sync_memory 的 delete_by_note → add 幂等补写。
"""
import json
import logging

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
from app.timeutil import to_utc, utcnow

logger = logging.getLogger(__name__)

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

    tzname = body.timezone or get_settings().default_timezone
    note = Note(
        id=new_id("nte"),
        user_id=user_id,
        client_note_id=body.client_note_id,
        text=body.text,
        source=body.source,
        # naive 的 recorded_at 按笔记时区本地化，不当成 UTC
        recorded_at=to_utc(body.recorded_at, tzname),
        timezone=tzname,
        status="uploaded",
        rev=1,
        # 先占位 0，业务行 INSERT 落定后再紧贴提交分配写入（加锁顺序规则）
        change_seq=0,
        asr_meta_json=_asr_json(body.asr),
    )
    session.add(note)
    try:
        # INSERT 业务行在前，allocate_seq 是事务里最后一把锁（全局规则，
        # 见 app/common.py）：与 PATCH/organize 等路径保持同一顺序
        await session.flush()
        note.change_seq = await allocate_seq(session)
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

    # 加锁顺序（全局规则见 app.common.allocate_seq）：先条件更新业务行
    # note（本条 UPDATE 拿行锁），再作废 reminders，最后才 allocate_seq
    # 用第二条 UPDATE 写 change_seq。旧实现把 allocate_seq 写在 .values()
    # 里（先锁 sequences 再锁 note），与 organize 写回的“先 FOR UPDATE
    # note 再 allocate_seq”重叠时在 PG 上构成 AB-BA 死锁
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
            # 旧记忆已随提交后的清理作废；新整理提交后
            # organize 会重新写入并把标记置回 True
            memory_synced=False,
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
    reminder_ids = await _invalidate_outputs(session, note.id)
    await enqueue(session, "organize_note", {"note_id": note_id, "rev": body.rev + 1})
    await _assign_change_seqs(session, note_id, reminder_ids)
    await session.commit()
    # 旧记忆在提交后清理（mem0 会调外部 LLM/embedding，不能放进事务）：
    # 失败只记日志——重新整理的 _sync_memory 本来就会先 delete_by_note
    # 再 add，这里只是整理最终失败时也不留旧记忆的兜底
    try:
        await get_memory_store().delete_by_note(user_id, note_id)
    except Exception:  # 记忆清理失败不能让已提交的 PATCH 变 500
        logger.warning("patch %s: 旧记忆清理失败（重整理会再写）", note_id, exc_info=True)
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
    # 加锁顺序（全局规则见 app.common.allocate_seq）：先条件更新业务行
    # note → 软删 reminders，最后才 allocate_seq 写 change_seq
    now = utcnow()
    result = await session.execute(
        update(Note)
        .where(Note.id == note_id, Note.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # 并发删除已先落定：按已删除处理
        await session.rollback()
        raise ApiError(404, "not_found", "笔记不存在")
    reminder_ids = await _tombstone_reminders(session, note.id, only_suggested=False)
    # 记忆清理兜底：幂等的 cleanup_memory job（笔记仍处于已删除才执行
    # delete_by_note）。即使下面提交后的直接清理失败、或与整理任务的
    # store.add 交错导致“删除后记忆复活”，也能最终清掉
    await enqueue(session, "cleanup_memory", {"note_id": note_id, "user_id": user_id})
    await _assign_change_seqs(session, note.id, reminder_ids)
    await session.commit()
    # 提交后直接清理（mem0 会调外部 LLM/embedding，不能放进事务）；
    # 失败只记日志，cleanup_memory job 会兜底
    try:
        await get_memory_store().delete_by_note(user_id, note_id)
    except Exception:  # 已提交的删除不能因记忆后端故障变 500
        logger.warning("delete %s: 记忆清理失败（cleanup_memory job 会兜底）", note_id, exc_info=True)


async def _tombstone_reminders(
    session: AsyncSession, note_id: str, only_suggested: bool
) -> list[str]:
    """条件软删该笔记的提醒（拿业务行锁），返回被删的提醒 id。

    change_seq 故意不在这里写：按加锁顺序规则，先锁完业务行
    （note → reminders），最后才 allocate_seq，再由 _assign_change_seqs
    用第二条 UPDATE 写回。
    """
    conditions = [Reminder.note_id == note_id, Reminder.deleted_at.is_(None)]
    if only_suggested:
        conditions.append(Reminder.status == "suggested")
    result = await session.execute(
        update(Reminder)
        .where(*conditions)
        .values(deleted_at=utcnow())
        .returning(Reminder.id)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars().all())


async def _assign_change_seqs(
    session: AsyncSession, note_id: str, reminder_ids: list[str]
) -> None:
    """业务行锁齐之后批量分配并写回 change_seq（必须最后调用，紧贴提交）。

    每行独占一个 seq：共用同一个值时，分页切在组中间会让剩余墓碑被
    `> cursor` 永久跳过（见 app/api/sync.py）。
    """
    total = 1 + len(reminder_ids)
    last_seq = await allocate_seq(session, count=total)
    seqs = iter(range(last_seq - total + 1, last_seq + 1))
    # 第二条 UPDATE 只写 change_seq：行锁已在第一条条件 UPDATE 里拿到
    await session.execute(
        update(Note).where(Note.id == note_id).values(change_seq=next(seqs))
    )
    for reminder_id in reminder_ids:
        await session.execute(
            update(Reminder).where(Reminder.id == reminder_id).values(change_seq=next(seqs))
        )


async def _invalidate_outputs(session: AsyncSession, note_id: str) -> list[str]:
    """重新整理前作废旧的建议提醒（返回被软删的提醒 id）。

    已确认（scheduled 等）的提醒不动。记忆不在这里删：清理移到提交之后
    由调用方执行（mem0 会调外部服务，不能放进持锁事务）。
    """
    return await _tombstone_reminders(session, note_id, only_suggested=True)
