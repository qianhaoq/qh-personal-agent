"""笔记接口：幂等创建、读取、乐观锁更新、软删除。

加锁顺序（防 PG 死锁，规则见 app/common.py 的 allocate_seq docstring 与
docs/server-invariants.md INV-1）：所有写路径都是先锁业务行（note 的条件
UPDATE → reminders 的条件 UPDATE），再 INSERT job 行（一律新行、
memory_sync 用 ON CONFLICT DO NOTHING，绝不 UPDATE 既存 job 行），全部
flush 落定之后最后 allocate_seq，再用第二条 UPDATE 写 change_seq。

记忆（后端内部还会调 LLM/embedding）一律不在这里写删（INV-4）：
PATCH/DELETE 只在写事务内 enqueue memory_sync，由该 job 按 note 当前态
幂等收敛（见 app/pipeline/memory_sync.py）。
"""
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import allocate_seq, new_id
from app.config import get_settings
from app.db import get_db_session
from app.deps import get_current_user_id
from app.errors import ApiError
from app.jobs.queue import enqueue
from app.models import Note, Reminder
from app.schemas import NoNulStr, NoteCreate, NotePatch, note_to_dict
from app.timeutil import to_utc, utcnow

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
        # 业务行（note 新行 → jobs 新行）的 INSERT 全部先 flush 落定，之后
        # allocate_seq 才是事务里最后一把锁（全局规则，见 app/common.py；
        # job INSERT 在 seq 分配之前，修 C2）。与 PATCH/extract 等路径同序
        await session.flush()
        await enqueue(
            session,
            "extract",
            {"note_id": note.id, "rev": 1},
            note_id=note.id,
            note_rev=1,
        )
        note.change_seq = await allocate_seq(session)
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
    # change_seq 是 int8：上界钉住，超大整数绑定 SQL 会 OverflowError → 500
    cursor: int = Query(default=0, ge=0, le=2**63 - 1),
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
    note_id: Annotated[NoNulStr, Path()],
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    note = await _get_owned_note(session, user_id, note_id)
    return note_to_dict(note)


async def _raise_conflict_or_not_found(
    session: AsyncSession, user_id: str, note_id: str
) -> None:
    """条件更新匹配 0 行后的归因（INV-11/INV-12）：回滚后重读当前状态，
    区分 404（不存在/已删除/他人笔记）与 409（rev 已被并发修改）。"""
    current = (
        await session.execute(select(Note).where(Note.id == note_id, Note.user_id == user_id))
    ).scalar_one_or_none()
    if current is None or current.deleted_at is not None:
        raise ApiError(404, "not_found", "笔记不存在")
    raise ApiError(409, "conflict", "版本冲突，请拉取最新版本后重试")


@router.patch("/{note_id}")
async def patch_note(
    note_id: Annotated[NoNulStr, Path()],
    body: NotePatch,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    # 先读只做存在性/归属检查；真正的乐观锁靠下面的条件更新原子完成，
    # 避免“读到 rev → 修改 → 写回”两步之间被并发请求插队
    note = await _get_owned_note(session, user_id, note_id)
    if body.text is None or body.text == note.text:
        # 内容不变的快路径也必须先原子校验 rev（INV-11，threads6 P1-2）：
        # 拿过期 rev 的无操作 PATCH 是 409 而不是 200——否则客户端会误以为
        # 自己的过期视图就是当前态。自赋值 UPDATE 只取行锁、不改任何列
        # （显式赋值抑制列级 onupdate，不产生可观察时间戳），rowcount=1
        # 即「锁内 rev 仍匹配」的原子判定；0 行回滚重读归因（INV-12）
        checked = await session.execute(
            update(Note)
            .where(
                Note.id == note_id,
                Note.user_id == user_id,
                Note.rev == body.rev,
                Note.deleted_at.is_(None),
            )
            .values(updated_at=Note.updated_at)
            .execution_options(synchronize_session=False)
        )
        if checked.rowcount == 0:
            await session.rollback()
            await _raise_conflict_or_not_found(session, user_id, note_id)
        # 持锁后的权威读：期间若有变更恰好落到同一 rev 且 text 已不同，
        # 不能谎报内容一致——落回完整 PATCH 路径（其条件更新会再次校验）
        await session.refresh(note)
        if body.text is None or body.text == note.text:
            await session.commit()
            return {"note": note_to_dict(note), "rev": note.rev}

    # 加锁顺序（全局规则见 app.common.allocate_seq）：先条件更新业务行
    # note（本条 UPDATE 拿行锁），再作废 reminders，再 INSERT job 行
    # （extract + memory_sync，均新行），全部 flush 后最后 allocate_seq，
    # 用第二条 UPDATE 写 change_seq
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
            # 旧 rev 的记忆已作废：由本次入队的 memory_sync 清掉（extract
            # 成功后再写新 rev 的记忆）
            memory_status="pending",
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        await session.rollback()
        # 复查区分 404（被并发删除/不存在）与 409（rev 已被并发修改）
        await _raise_conflict_or_not_found(session, user_id, note_id)

    # 旧的整理产物作废，重新入队 extract（带新 rev，执行侧据此丢弃过期
    # 任务）；同时入队 memory_sync 清旧记忆——即使 extract 永久失败，
    # 旧 rev 的记忆也会被清掉（修 C4），且这里不再提交后直删记忆
    # （记忆后端会调外部服务，也不能放进事务；入口唯一化见 INV-4）
    reminder_ids = await _invalidate_outputs(session, note.id)
    await enqueue(
        session,
        "extract",
        {"note_id": note_id, "rev": body.rev + 1},
        note_id=note.id,
        note_rev=body.rev + 1,
    )
    await enqueue(
        session, "memory_sync", {"note_id": note_id}, note_id=note.id, dedupe_pending=True
    )
    await _assign_change_seqs(session, note_id, reminder_ids)
    await session.commit()
    # expire_on_commit=False：显式刷新让 note_to_dict 读到条件更新后的行
    await session.refresh(note)
    return {"note": note_to_dict(note), "rev": note.rev}


@router.delete("/{note_id}", status_code=204)
async def delete_note(
    note_id: Annotated[NoNulStr, Path()],
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
):
    note = await _get_owned_note(session, user_id, note_id)
    # 加锁顺序（全局规则见 app.common.allocate_seq）：先条件更新业务行
    # note → 软删 reminders → INSERT memory_sync（新行，清记忆的唯一
    # 入口）→ flush → 最后 allocate_seq 写 change_seq
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
    # 记忆清理由 memory_sync job 收敛（与任何在途的整理交错也不会复活或
    # 误删，见 app/pipeline/memory_sync.py 的复查协议）
    await enqueue(
        session, "memory_sync", {"note_id": note_id}, note_id=note.id, dedupe_pending=True
    )
    await _assign_change_seqs(session, note.id, reminder_ids)
    await session.commit()


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

    已确认（scheduled 等）的提醒不动。记忆不在这里删（INV-4）：
    清理由 PATCH 入队的 memory_sync job 负责。
    """
    return await _tombstone_reminders(session, note_id, only_suggested=True)
