"""memory_sync job 执行体：记忆写删的**唯一入口**（INV-4/INV-5）。

API 请求路径（PATCH/DELETE note）与 extract job 都不直接改记忆，只能在
写事务内 enqueue memory_sync；本模块（与未来的账户级 delete_all）是仅有的
MemoryStore.add / delete / delete_by_note 调用方。

单次执行的固定四步（INV-5，按 note_id 幂等收敛到「记忆 == f(note 终态)」）：

1. **读**：短读事务读 note 的 (deleted_at, rev, text, status, user_id)，
   随即提交，不持锁、不占序列。
2. **写外部**（事务外，无任何 DB 会话——mem0 内部还有 LLM/embedding）：
   已删除 → 只 delete_by_note；未删除且 organized → delete_by_note 后
   add(text, metadata={note_id, source_rev=rev})，记录本次 add 返回的全部
   memory id；未删除且未 organized（uploaded/failed）→ 只 delete_by_note、
   不 add——清掉属于旧 rev 的残留记忆（PATCH 后 extract 永久失败时旧记忆
   必须消失，修 C4），新记忆等 extract 成功后由其入队的任务补写。
3. **复查**（threads5 P2-1 起三结局）：新短读事务重读 (rev, deleted_at,
   status) **并确认自己仍持有租约**（job 行 status='running' AND
   locked_by=:worker_id AND locked_at=:claimed）。租约仍在且与第 1 步相同 →
   条件 UPDATE memory_status（'synced'；note 已 failed 且记忆为空则
   'failed'——该状态下不可能再有记忆），条件 id+rev+未删除，不占 seq、
   不碰 updated_at/change_seq（INV-2.6）；租约仍在但状态已变 → **只删本次
   add 产生的 memory id**（按 id 删，绝不 delete_by_note，修 C9：不误删
   并发新 rev 任务刚写入的记忆）+ INSERT memory_sync … ON CONFLICT DO
   NOTHING 再入队（与变更事务已入队的 pending 去重合并），job 仍算 done；
   **租约已丢（被回收换主）→ 只删本次 add 产生的 id，不置位、不再入队**
   ——外部写超过锁超时后任务会被回收，迟到 worker 不得仅凭 (rev,
   deleted_at) 未变就再写一组记忆/置位。
4. **收尾**：runner 的 finalize_claim 只校验租约（INV-6），不带 rev。

复查键 (rev, deleted_at) 足够：status 无法在不改 rev/deleted_at 的前提下
变化（PATCH 改 rev，DELETE 改 deleted_at，apply 改 rev）；唯一例外是
extract 失败标记（uploaded→failed 不动 rev），由第 3 步读出的 status 分流。

串行化：同 note 同时至多一个 running 由部分唯一索引硬保证（方案 C，见
INV-5 的评估）；但正确性不依赖串行化——复查/删己/再入队保证任何交错最终
收敛（论证见 docs/server-invariants.md INV-5）。
"""
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_sessionmaker
from app.jobs.queue import enqueue
from app.memory.base import MemoryItem, get_memory_store
from app.models import Job, Note
from app.timeutil import as_utc


async def sync_note_memory(
    note_id: str,
    *,
    job_id: str | None = None,
    worker_id: str | None = None,
    locked_at: datetime | None = None,
) -> None:
    """把一条笔记的记忆收敛到「记忆 == f(note 当前态)」。抛异常由调用方
    （runner）决定重试；重放幂等（先 delete_by_note 再 add）。

    job_id/worker_id/locked_at 是认领快照里的租约凭证（threads5 P2-1）：
    复查时除 (rev, deleted_at) 外还必须确认自己仍持有租约——外部写
    （mem0 的 LLM/embedding）超过锁超时后任务会被回收换主，仅比
    (rev, deleted_at) 挡不住「复活慢 worker 的迟到写入」。不带租约信息
    的调用（测试/未来账户级路径）按无栅栏处理。"""
    maker = get_sessionmaker()

    # 1. 短读事务：读完即提交，不持锁
    async with maker() as session:
        note = await session.get(Note, note_id)
    if note is None:
        return  # 行不存在（理论不可达，软删不删行）：无事可做
    user_id = note.user_id
    was_deleted = note.deleted_at is not None
    rev = note.rev
    text = note.text
    organized = note.status == "organized"

    # 2. 写外部：无任何数据库会话/锁
    store = get_memory_store()
    await store.delete_by_note(user_id, note_id)
    added: list[MemoryItem] = []
    if not was_deleted and organized:
        added = await store.add(user_id, text, note_id=note_id, source_rev=rev)

    # 3. 复查：新短读事务重读 (rev, deleted_at, status) + job 行租约
    async with maker() as session:
        again = await session.get(Note, note_id)
        job_row = await session.get(Job, job_id) if job_id is not None else None
    lease_ok = (
        job_id is None  # 无租约上下文（测试/账户级路径）：无栅栏
        or (
            job_row is not None
            and job_row.status == "running"
            and worker_id is not None
            and job_row.locked_by == worker_id
            and locked_at is not None
            and as_utc(job_row.locked_at) == locked_at
        )
    )
    unchanged = (
        again is not None
        and (again.deleted_at is not None) == was_deleted
        and again.rev == rev
    )

    if not lease_ok:
        # 租约已丢（被回收换主）：只删本次 add 产生的 id，不置位、不再入队
        # ——job 已由新持有者接管，其执行会按 note 当前状态收敛；迟到的
        # 本次执行不得再留下任何对外效果（threads5 P2-1 / INV-6）
        if added:
            await store.delete(user_id, [m.id for m in added])
        return

    if unchanged:
        # 已删除的终态：条件更新自然不命中（deleted_at IS NULL），记忆已清
        await _mark_memory_status(maker, note_id, rev, failed=again.status == "failed")
        return

    # 状态已变：只删本次 add 产生的 memory id（修 C9），再入队（pending
    # 去重合并），由下一个任务按新状态收敛；本 job 仍算 done
    if added:
        await store.delete(user_id, [m.id for m in added])
    async with maker() as session, session.begin():
        await enqueue(
            session, "memory_sync", {"note_id": note_id}, note_id=note_id, dedupe_pending=True
        )


async def _mark_memory_status(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    rev: int,
    *,
    failed: bool,
) -> None:
    """置 memory_status（服务端内部状态，不进 API 响应）：不占 change_seq、
    也不碰 updated_at（INV-2.6——`updated_at=Note.updated_at` 自赋值抑制
    列级 onupdate，否则会产生客户端永远观察不到的时间戳）。条件 id + rev +
    未删除：期间又被 PATCH/DELETE 时不置位，交给重新入队的任务。"""
    async with maker() as session, session.begin():
        await session.execute(
            update(Note)
            .where(Note.id == note_id, Note.rev == rev, Note.deleted_at.is_(None))
            .values(
                memory_status="failed" if failed else "synced",
                updated_at=Note.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
