"""memory_sync job 执行体：记忆写删的**唯一入口**（INV-4/INV-5）。

API 请求路径（PATCH/DELETE note）与 extract job 都不直接改记忆，只能在
写事务内 enqueue memory_sync；本模块（与未来的账户级 delete_all）是仅有的
MemoryStore.add / delete / delete_by_note 调用方。

单次执行的固定四步（INV-5，按 note_id 幂等收敛到「记忆 == f(note 终态)」）：

1. **读**：短读事务读 note 的 (deleted_at, rev, text, status, user_id)，
   随即提交，不持锁、不占序列。
2. **写外部**（事务外，无任何 DB 会话——记忆后端内部还有 LLM/embedding）：
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
   不碰 updated_at/change_seq（INV-2.6）；**置位事务内再原子复核一次租约**
   （threads8 P2 / INV-13：本步的复核会话已提交，若协程在两步之间暂停、
   任务被回收换主，迟到的 worker 仍会写下 synced 掩盖未同步状态——置位
   与租约判定必须同事务原子完成，复核不过不置位、静默让位给后继任务）；
   租约仍在但状态已变 → **只删本次
   add 产生的 memory id**（按 id 删，绝不 delete_by_note，修 C9：不误删
   并发新 rev 任务刚写入的记忆）+ INSERT memory_sync … ON CONFLICT DO
   NOTHING 再入队（与变更事务已入队的 pending 去重合并），job 仍算 done；
   **租约已丢（被回收换主）→ 只删本次 add 产生的 id、不置位，然后同样
   无条件再入队一次补偿任务**（threads6 P1-1）——第 2 步的 delete_by_note
   本身也可能耗过锁超时、在换主执行完成**之后**才落地，把新持有者刚写的
   记忆删掉而队列里再无后继任务（note 永久无记忆、memory_status=synced）；
   「删己 id」只对账自己 add 的部分，迟到删除造成的损害只能靠补偿再入队
   兜底。本执行的全部外部写（delete_by_note + add）在复查前已顺序完成，
   补偿任务在其后执行、读到最新状态幂等收敛；它拿新租约，若再丢租约同样
   再入队——每轮至多一个 pending（部分唯一索引），收敛有界。
4. **收尾**：runner 的 finalize_claim 只校验租约（INV-6），不带 rev。

复查键 (rev, deleted_at) 足够：status 无法在不改 rev/deleted_at 的前提下
变化（PATCH 改 rev，DELETE 改 deleted_at，apply 改 rev）；唯一例外是
extract 失败标记（uploaded→failed 不动 rev），由第 3 步读出的 status 分流。

串行化：同 note 同时至多一个 running 由部分唯一索引硬保证（方案 C，见
INV-5 的评估）；但正确性不依赖串行化——复查/删己/再入队保证任何交错最终
收敛（论证见 docs/server-invariants.md INV-5）。
"""
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_sessionmaker
from app.jobs.queue import enqueue, is_postgres
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
    （记忆后端的 LLM/embedding）超过锁超时后任务会被回收换主，仅比
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
        # 租约已丢（被回收换主）：只删本次 add 产生的 id、不置位，但**必须
        # 补偿再入队**（threads6 P1-1 / INV-5）：第 2 步的 delete_by_note 可能
        # 在换主执行完成之后才落地，把新持有者写的记忆删掉且再无后继任务
        # ——「删己 id」对账不了这部分损害，唯一可靠的兜底是让队列再收敛
        # 一次（ON CONFLICT DO NOTHING 与已有 pending 去重；迟到执行不得
        # 置位/写 note，但允许留下这一个 INSERT，见 INV-6）。
        # 删己 id 与补偿入队必须 try/finally（threads7 P1 / INV-5）：本执行
        # 租约已丢、收尾会被 finalize_claim 放弃，清理抛异常再吞掉入队就
        # 再无任何收敛途径——异常照常上抛，入队绝不缺席
        try:
            if added:
                await store.delete(user_id, [m.id for m in added])
        finally:
            await _compensating_requeue(maker, note_id)
        return

    if unchanged:
        # 已删除的终态：条件更新自然不命中（deleted_at IS NULL），记忆已清
        # 置位事务内自带原子租约复核（见 _mark_memory_status）
        await _mark_memory_status(
            maker,
            note_id,
            rev,
            failed=again.status == "failed",
            job_id=job_id,
            worker_id=worker_id,
            locked_at=locked_at,
        )
        return

    # 状态已变：只删本次 add 产生的 memory id（修 C9），再入队（pending
    # 去重合并），由下一个任务按新状态收敛；本 job 仍算 done。同丢租约
    # 路径：删己 id 抛异常也必须留下后继任务（threads7 P1 / INV-5），
    # 异常上抛由 runner 记录错误并按租约收尾（重试或让位给 pending）
    try:
        if added:
            await store.delete(user_id, [m.id for m in added])
    finally:
        await _compensating_requeue(maker, note_id)


async def _compensating_requeue(
    maker: async_sessionmaker[AsyncSession], note_id: str
) -> None:
    """补偿再入队（INV-5）：与调用方的「删己 id」清理构成 try/finally 的
    finally 侧——清理抛异常也一定入队（ON CONFLICT DO NOTHING 与既有
    pending 去重合并）。"""
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
    job_id: str | None = None,
    worker_id: str | None = None,
    locked_at: datetime | None = None,
) -> bool:
    """置 memory_status（服务端内部状态，不进 API 响应）：不占 change_seq、
    也不碰 updated_at（INV-2.6——`updated_at=Note.updated_at` 自赋值抑制
    列级 onupdate，否则会产生客户端永远观察不到的时间戳）。条件 id + rev +
    未删除：期间又被 PATCH/DELETE 时不置位，交给重新入队的任务。

    threads8 P2 / INV-5/6/13：置位必须与租约复核**同事务原子**——调用方的
    复查会话在到达这里之前已提交，若协程在两步之间暂停、租约被回收换主，
    只比 (id, rev, 未删除) 的迟到 worker 仍会写下 synced 掩盖未同步状态。
    本事务内先锁 job 行（PG FOR UPDATE；锁序 = 队列路径 jobs → note，见
    INV-1 表，业务路径对 jobs 只 INSERT 不 UPDATE，无 AB-BA），锁内复核
    租约——取行后在 Python 里比 as_utc(locked_at)：SQLite 以字符串存
    datetime，SQL 等值比较对 aware 参数不可靠。复核不过**不置位、静默
    让位**（回收者必有后继：换主执行或回收后 pending 再入队都会再收敛，
    同 finalize_claim 的语义）；无租约上下文（测试/账户级路径）按无栅栏
    处理。返回是否实际置位。"""
    async with maker() as session, session.begin():
        if job_id is not None:
            stmt = select(Job).where(Job.id == job_id)
            if is_postgres():
                stmt = stmt.with_for_update()
            job_row = (await session.execute(stmt)).scalar_one_or_none()
            lease_ok = (
                job_row is not None
                and job_row.status == "running"
                and worker_id is not None
                and job_row.locked_by == worker_id
                and locked_at is not None
                and as_utc(job_row.locked_at) == locked_at
            )
            if not lease_ok:
                return False
        await session.execute(
            update(Note)
            .where(Note.id == note_id, Note.rev == rev, Note.deleted_at.is_(None))
            .values(
                memory_status="failed" if failed else "synced",
                updated_at=Note.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    return True
