"""基于数据库 jobs 表的任务队列。

- PostgreSQL：SELECT ... FOR UPDATE SKIP LOCKED，多 worker 安全并发。
- SQLite：没有行锁，退化为“先取候选、再条件 UPDATE 抢占”的普通事务
  （单写者模型，测试与本地开发够用）。

任务模型（INV-2，docs/server-invariants.md）：kind ∈ {extract, memory_sync}。

- extract 携带 note_rev（入队时的笔记版本），执行时 note.rev 不一致即跳过
  = done（过期任务无副作用）；不做 pending 去重（同 note 多个不同 rev 的
  pending extract 合法，旧 rev 执行时自跳过）。
- memory_sync 不带 rev，按 note 当前状态收敛记忆；入队用 INSERT ... ON
  CONFLICT DO NOTHING 做 pending 去重；同 note 同时至多一个 running 由
  部分唯一索引 uq_jobs_memsync_running 在提交时硬保证——claim 快路径先
  用 NOT EXISTS 过滤“已有 running 同 note memory_sync”的候选，漏网时
  （两 worker 并发置 running）必有一方在唯一索引上 IntegrityError，
  savepoint 回滚该候选继续找下一个（方案 C，见 INV-5 的评估）。

领取规则（P1-B）：
- status=pending 且 run_after<=now：正常到期执行；
- status=running 且 locked_at < now - JOB_LOCK_TIMEOUT_SECONDS：worker
  崩溃/重启遗留的任务，锁过期后回收重跑（attempts 照常 +1）；仅当
  attempts 已达 max_attempts（最后一次允许的尝试也已用过）才不再执行，
  直接标 failed，并按 kind 分流标记 note（extract → note.status=failed
  （入队 rev 条件）；memory_sync → memory_status=failed（无其他
  pending/running 条件），见 mark_note_failed_on_job_failure）。

锁顺序（INV-1 队列路径，单向 jobs → note（仅失败标记）→ sequences →
change_seq 写回，与业务路径（note → … → jobs → sequences）不相交、无环）：
既存 job 行的 UPDATE 只发生在本模块（claim / finalize / 回收判死）。

租约（P2）：领取时写入 locked_by（worker 实例 id）与 locked_at，收尾
（finalize_claim）按两者 + status='running' 条件校验——任务被回收给
别的 worker 后，旧 worker 的收尾一律放弃，不再改动 job/note。

用法：
    await enqueue(session, "extract", {"note_id": "...", "rev": 1},
                  note_id="...", note_rev=1)
    async with claim_next(session, worker_id="wkr_x") as job:
        if job is None: ...  # 队列空
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import and_, literal, not_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.common import allocate_seq, new_id
from app.models import Job, Note
from app.timeutil import as_utc, utcnow


def new_worker_id() -> str:
    """worker 实例标识：写入 jobs.locked_by，收尾校验租约用。"""
    return f"wkr_{uuid.uuid4().hex}"


def is_postgres() -> bool:
    from app.config import get_settings

    return get_settings().database_url.startswith("postgresql")


def _lock_cutoff(now: datetime) -> datetime:
    """running 状态的锁过期时刻：locked_at 早于它即视为 worker 已中断。"""
    from app.config import get_settings

    return now - timedelta(seconds=get_settings().job_lock_timeout_seconds)


def _running_sibling_exists() -> bool:
    """SQL 表达式：同 note 是否已有别的 running memory_sync（claim 快路径过滤）。"""
    sibling = aliased(Job)
    return (
        select(literal(1))
        .where(
            sibling.kind == "memory_sync",
            sibling.note_id == Job.note_id,
            sibling.status == "running",
            sibling.id != Job.id,
        )
        .exists()
        .correlate(Job)
    )


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict,
    *,
    note_id: str | None = None,
    note_rev: int | None = None,
    dedupe_pending: bool = False,
) -> str | None:
    """入队一个 job。返回 job id；dedupe_pending 且被去重（已有同 note 的
    pending memory_sync）时返回 None。

    业务路径对既存 job 行只有 INSERT（INV-1.3）：memory_sync 的 pending
    去重靠部分唯一索引 + ON CONFLICT DO NOTHING，绝不 UPDATE 既存行。
    """
    from app.config import get_settings

    values = {
        "id": new_id("job"),
        "kind": kind,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "status": "pending",
        "max_attempts": get_settings().job_max_attempts,
        "note_id": note_id,
        "note_rev": note_rev,
    }
    if dedupe_pending:
        # 同 note 已有 pending 的 memory_sync 时静默跳过（pending 唯一性只
        # 约束 pending 之间；running 期间再入队允许新建 pending）
        if is_postgres():
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(Job).values(**values).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(Job).values(**values).on_conflict_do_nothing()
        result = await session.execute(stmt)
        return values["id"] if result.rowcount else None
    session.add(Job(**values))
    await session.flush()
    return values["id"]


@asynccontextmanager
async def claim_next(session: AsyncSession, worker_id: str | None = None):
    """领取一个到期/可回收的 job（None 表示没有可执行的）。

    worker_id 写入 jobs.locked_by 作为租约持有者；缺省时每次生成一次性
    id（等价于“领取后即失联”的 worker，回收语义不受影响）。调用方负责
    在同一个事务里完成领取，并用 finalize_claim 做带租约校验的收尾。
    """
    now = utcnow()
    wid = worker_id or new_worker_id()
    job = (
        await _claim_postgres(session, now, wid)
        if is_postgres()
        else await _claim_sqlite(session, now, wid)
    )
    yield job


async def _claim_postgres(session: AsyncSession, now: datetime, worker_id: str) -> Job | None:
    cutoff = _lock_cutoff(now)
    while True:
        row = (
            await session.execute(
                text(
                    "SELECT j.id, j.status, j.attempts, j.max_attempts, j.payload_json, "
                    "j.kind, j.note_id, j.note_rev FROM jobs j WHERE ("
                    "((j.status = 'pending' AND j.run_after <= :now) OR "
                    "(j.status = 'running' AND j.locked_at < :cutoff)) "
                    "AND NOT (j.kind = 'memory_sync' AND EXISTS ("
                    "  SELECT 1 FROM jobs s WHERE s.kind = 'memory_sync' "
                    "  AND s.note_id = j.note_id AND s.status = 'running' "
                    "  AND s.id != j.id))) "
                    "ORDER BY j.run_after, j.created_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED"
                ),
                {"now": now, "cutoff": cutoff},
            )
        ).fetchone()
        if row is None:
            return None
        job_id, status, attempts, max_attempts, _payload_json = row[:5]
        kind, note_id, note_rev = row[5], row[6], row[7]
        if status == "pending":
            job = await _mark_running(session, job_id, now, worker_id)
        elif attempts >= max_attempts:
            # running 锁过期且次数已用尽（含最后一次允许的尝试）：不再执行，直接判死
            await _fail_expired(
                session, job_id, kind=kind, note_id=note_id, note_rev=note_rev
            )
            job = None
        else:
            job = await _reclaim_running(session, job_id, now, cutoff, worker_id)
        if job is not None:
            return job
        # 判死/抢占/唯一索引冲突未成功：继续找下一个候选


async def _claim_sqlite(session: AsyncSession, now: datetime, worker_id: str) -> Job | None:
    # 时间过滤放在 Python 里做：SQLite 读回的 naive UTC 和 aware now 不能直接比
    cutoff = _lock_cutoff(now)
    rows = (
        await session.execute(
            select(
                Job.id,
                Job.status,
                Job.run_after,
                Job.locked_at,
                Job.attempts,
                Job.max_attempts,
                Job.payload_json,
                Job.kind,
                Job.note_id,
                Job.note_rev,
            )
            .where(
                Job.status.in_(("pending", "running")),
                # 快路径：同 note 已有 running 的 memory_sync 时不领（真正的
                # 互斥由部分唯一索引在写入时保证，SQLite 读写窗口更宽）
                not_(and_(Job.kind == "memory_sync", _running_sibling_exists())),
            )
            .order_by(Job.run_after, Job.created_at)
        )
    ).fetchall()
    for job_id, status, run_after, locked_at, attempts, max_attempts, payload_json, kind, note_id, note_rev in rows:
        if status == "pending":
            if as_utc(run_after) <= now:
                job = await _mark_running(session, job_id, now, worker_id)
                if job is not None:
                    return job
            continue
        # running：锁过期才能回收；locked_at 为 NULL 属于异常数据，保守跳过
        if locked_at is None or as_utc(locked_at) >= cutoff:
            continue
        if attempts >= max_attempts:
            await _fail_expired(session, job_id, kind=kind, note_id=note_id, note_rev=note_rev)
            continue
        job = await _reclaim_running(session, job_id, now, cutoff, worker_id)
        if job is not None:
            return job
    return None


async def _mark_running(
    session: AsyncSession, job_id: str, now: datetime, worker_id: str
) -> Job | None:
    # savepoint：两个 worker 并发把同 note 的两个 pending memory_sync 置
    # running 时，必有一方在 uq_jobs_memsync_running 上 IntegrityError——
    # 只回滚这一个候选，事务继续找下一个（INV-5 方案 C）
    try:
        async with session.begin_nested():
            result = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "pending")
                .values(status="running", attempts=Job.attempts + 1, locked_at=now, locked_by=worker_id)
            )
            claimed = result.rowcount
    except IntegrityError:
        return None
    if not claimed:  # 已被别人抢走
        return None
    await session.flush()
    return await session.get(Job, job_id)


async def _reclaim_running(
    session: AsyncSession, job_id: str, now: datetime, cutoff: datetime, worker_id: str
) -> Job | None:
    """回收锁过期的 running 任务（attempts +1、换租约）。条件更新防并发抢占。"""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.locked_at < cutoff)
        .values(attempts=Job.attempts + 1, locked_at=now, locked_by=worker_id)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:  # 已被别人回收
        return None
    await session.flush()
    return await session.get(Job, job_id)


async def _fail_expired(
    session: AsyncSession,
    job_id: str,
    *,
    kind: str | None,
    note_id: str | None,
    note_rev: int | None,
) -> None:
    """锁过期且次数用尽的 running 任务：判 failed，并按 kind 分流标记 note。"""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(status="failed", last_error="running 锁过期（worker 中断）且重试次数用尽")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        await mark_note_failed_on_job_failure(
            session, kind=kind, note_id=note_id, note_rev=note_rev, job_id=job_id
        )
    await session.flush()


async def mark_note_failed_on_job_failure(
    session: AsyncSession,
    *,
    kind: str | None,
    note_id: str | None,
    note_rev: int | None,
    job_id: str,
) -> bool:
    """job 判 failed 时按 kind 分流标记 note（两类失败语义互不污染，修 C5）：

    - extract 耗尽 → note.status='failed'，条件 `rev=入队rev AND
      deleted_at IS NULL AND status!='organized'`（用户 PATCH 过 / 已删除 /
      已整理成功时不覆盖）。
    - memory_sync 耗尽 → note.memory_status='failed'，条件：note 未删除且
      **无其他** pending/running 的 memory_sync（NOT EXISTS ... id != 本
      job）——还有后继任务在跑时失败只是中间态，不落 failed。不占 seq。

    锁序（队列路径，INV-1）：jobs 行 → note 条件 UPDATE → allocate_seq →
    change_seq 写回，单向不与业务路径成环。
    """
    if not note_id:
        return False
    if kind == "extract":
        return await _mark_extract_failed(session, note_id=note_id, note_rev=note_rev)
    if kind == "memory_sync":
        return await _mark_memory_sync_failed(session, note_id=note_id, job_id=job_id)
    return False


async def _mark_extract_failed(
    session: AsyncSession, *, note_id: str, note_rev: int | None
) -> bool:
    """extract 耗尽：条件 UPDATE note.status='failed' → allocate_seq → change_seq 写回。"""
    if note_rev is None:
        return False
    result = await session.execute(
        update(Note)
        .where(
            Note.id == note_id,
            Note.rev == note_rev,
            Note.deleted_at.is_(None),
            Note.status != "organized",
        )
        .values(status="failed", updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return False
    seq = await allocate_seq(session)
    await session.execute(
        update(Note).where(Note.id == note_id).values(change_seq=seq)
    )
    return True


async def _mark_memory_sync_failed(session: AsyncSession, *, note_id: str, job_id: str) -> bool:
    """memory_sync 耗尽：note.memory_status='failed'（无后继任务时），不占
    seq、不碰 updated_at（INV-2.6：内部状态更新不得产生客户端观察不到的
    时间戳——`updated_at=Note.updated_at` 自赋值抑制列级 onupdate）。"""
    others = (
        select(literal(1))
        .where(
            Job.kind == "memory_sync",
            Job.note_id == note_id,
            Job.status.in_(("pending", "running")),
            Job.id != job_id,
        )
        .exists()
    )
    result = await session.execute(
        update(Note)
        .where(Note.id == note_id, Note.deleted_at.is_(None), ~others)
        .values(memory_status="failed", updated_at=Note.updated_at)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def finalize_claim(
    session: AsyncSession,
    job_id: str,
    *,
    worker_id: str,
    locked_at: datetime,
    error: str | None,
    retry_delay_seconds: int = 30,
) -> str | None:
    """收尾一个已执行的 job：成功标 done，失败退避重试或达上限标 failed。

    租约校验（P2）：只对仍处于 running 且 locked_by/locked_at 与领取时
    一致的 job 收尾。任务在执行期间被回收给别的 worker（锁过期）时，
    本 worker 的收尾一律放弃（返回 None）——job 的状态由新持有者决定，
    领取方也不再基于过期结果修改 note。

    失败重试的 running→pending 回退对 memory_sync 可能撞 pending 唯一
    索引（执行期间 PATCH/DELETE 已入队新的 pending）：此时本 job 标
    done 让位，由那个 pending 重做（memory_sync 按 note 幂等收敛）。

    PG 下先 FOR UPDATE 拿行锁再检查：检查与写入之间不会被回收路径
    （条件 UPDATE running 行）插队；SQLite 单写者模型天然串行。
    """
    stmt = select(Job).where(Job.id == job_id)
    if is_postgres():
        stmt = stmt.with_for_update()
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None
    if (
        job.status != "running"
        or job.locked_by != worker_id
        or as_utc(job.locked_at) != as_utc(locked_at)
    ):
        return None  # 租约已失效（被回收/换主）：放弃收尾
    if error is None:
        job.status = "done"
    else:
        job.last_error = error[:2000]
        if job.attempts < job.max_attempts:
            try:
                async with session.begin_nested():
                    job.status = "pending"
                    job.run_after = utcnow() + timedelta(seconds=retry_delay_seconds)
                    await session.flush()
            except IntegrityError:
                # memory_sync：已有 pending 后继，让位标 done
                job.status = "done"
        else:
            job.status = "failed"
    await session.flush()
    if job.status == "failed":
        await mark_note_failed_on_job_failure(
            session,
            kind=job.kind,
            note_id=job.note_id,
            note_rev=job.note_rev,
            job_id=job.id,
        )
        await session.flush()
    return job.status
