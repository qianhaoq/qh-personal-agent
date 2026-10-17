"""基于数据库 jobs 表的任务队列。

- PostgreSQL：SELECT ... FOR UPDATE SKIP LOCKED，多 worker 安全并发。
- SQLite：没有行锁，退化为“先取候选、再条件 UPDATE 抢占”的普通事务
  （单写者模型，测试与本地开发够用）。

领取规则（P1-B）：
- status=pending 且 run_after<=now：正常到期执行；
- status=running 且 locked_at < now - JOB_LOCK_TIMEOUT_SECONDS：worker
  崩溃/重启遗留的任务，锁过期后回收重跑（attempts 照常 +1）；若
  attempts+1 已达 max_attempts，不再执行，直接标 failed，并按入队 rev
  条件把 note.status 置 failed（见 mark_note_failed_if_rev）。

用法：
    await enqueue(session, "organize_note", {"note_id": "..."})
    async with claim_next(session) as job:
        if job is None: ...  # 队列空
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import allocate_seq, new_id
from app.models import Job, Note
from app.timeutil import as_utc, utcnow


def is_postgres() -> bool:
    from app.config import get_settings

    return get_settings().database_url.startswith("postgresql")


def _lock_cutoff(now: datetime) -> datetime:
    """running 状态的锁过期时刻：locked_at 早于它即视为 worker 已中断。"""
    from app.config import get_settings

    return now - timedelta(seconds=get_settings().job_lock_timeout_seconds)


async def enqueue(session: AsyncSession, kind: str, payload: dict) -> str:
    from app.config import get_settings

    job = Job(
        id=new_id("job"),
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="pending",
        max_attempts=get_settings().job_max_attempts,
    )
    session.add(job)
    await session.flush()
    return job.id


@asynccontextmanager
async def claim_next(session: AsyncSession):
    """领取一个到期/可回收的 job（None 表示没有可执行的）。

    调用方负责在同一个事务里处理并调用 mark_done / mark_failed。
    """
    now = utcnow()
    job = await _claim_postgres(session, now) if is_postgres() else await _claim_sqlite(session, now)
    yield job


async def _claim_postgres(session: AsyncSession, now: datetime) -> Job | None:
    cutoff = _lock_cutoff(now)
    while True:
        row = (
            await session.execute(
                text(
                    "SELECT id, status, attempts, max_attempts, payload_json FROM jobs WHERE ("
                    "(status = 'pending' AND run_after <= :now) OR "
                    "(status = 'running' AND locked_at < :cutoff)) "
                    "ORDER BY run_after, created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {"now": now, "cutoff": cutoff},
            )
        ).fetchone()
        if row is None:
            return None
        job_id, status, attempts, max_attempts, payload_json = row
        if status == "pending":
            job = await _mark_running(session, job_id, now)
        elif attempts + 1 >= max_attempts:
            # running 锁过期且重试次数用尽：不再执行，直接判死
            await _fail_expired(session, job_id, payload_json)
            job = None
        else:
            job = await _reclaim_running(session, job_id, now, cutoff)
        if job is not None:
            return job
        # 判死/抢占未成功：继续找下一个候选


async def _claim_sqlite(session: AsyncSession, now: datetime) -> Job | None:
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
            )
            .where(Job.status.in_(("pending", "running")))
            .order_by(Job.run_after, Job.created_at)
        )
    ).fetchall()
    for job_id, status, run_after, locked_at, attempts, max_attempts, payload_json in rows:
        if status == "pending":
            if as_utc(run_after) <= now:
                job = await _mark_running(session, job_id, now)
                if job is not None:
                    return job
            continue
        # running：锁过期才能回收；locked_at 为 NULL 属于异常数据，保守跳过
        if locked_at is None or as_utc(locked_at) >= cutoff:
            continue
        if attempts + 1 >= max_attempts:
            await _fail_expired(session, job_id, payload_json)
            continue
        job = await _reclaim_running(session, job_id, now, cutoff)
        if job is not None:
            return job
    return None


async def _mark_running(session: AsyncSession, job_id: str, now: datetime) -> Job | None:
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(status="running", attempts=Job.attempts + 1, locked_at=now)
    )
    if result.rowcount == 0:  # 已被别人抢走
        return None
    await session.flush()
    return await session.get(Job, job_id)


async def _reclaim_running(session: AsyncSession, job_id: str, now: datetime, cutoff: datetime) -> Job | None:
    """回收锁过期的 running 任务（attempts +1、续锁）。条件更新防并发抢占。"""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.locked_at < cutoff)
        .values(attempts=Job.attempts + 1, locked_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:  # 已被别人回收
        return None
    await session.flush()
    return await session.get(Job, job_id)


async def _fail_expired(session: AsyncSession, job_id: str, payload_json: str | None) -> None:
    """锁过期且次数用尽的 running 任务：判 failed，并按 rev 条件置 note 失败。"""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(status="failed", last_error="running 锁过期（worker 中断）且重试次数用尽")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        await mark_note_failed_if_rev(session, json.loads(payload_json or "{}"))
    await session.flush()


async def mark_note_failed_if_rev(session: AsyncSession, payload: dict) -> bool:
    """把 note.status 置 failed，仅当笔记仍处于入队时的 rev 且未删除。

    条件更新（P1-C）：rowcount=0 说明用户已 PATCH（rev 变化，重新入队的
    任务会负责）或已删除，此时不动 note，避免过期任务的失败覆盖新版本。
    加锁顺序（全局规则见 app.common.allocate_seq）：先条件更新业务行，
    命中后才 allocate_seq、用第二条 UPDATE 写 change_seq——未命中时也
    不会白分配 seq 留空洞。
    """
    note_id = payload.get("note_id")
    enqueued_rev = payload.get("rev")
    if not note_id or enqueued_rev is None:
        return False
    result = await session.execute(
        update(Note)
        .where(Note.id == note_id, Note.rev == enqueued_rev, Note.deleted_at.is_(None))
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


async def mark_done(session: AsyncSession, job: Job) -> None:
    job.status = "done"
    await session.flush()


async def mark_failed(
    session: AsyncSession, job: Job, error: str, retry: bool, retry_delay_seconds: int = 30
) -> None:
    """失败处理：可重试且未超次数则回到 pending（run_after 推迟），否则 failed。"""
    job.last_error = error[:2000]
    if retry and job.attempts < job.max_attempts:
        job.status = "pending"
        job.run_after = utcnow() + timedelta(seconds=retry_delay_seconds)
    else:
        job.status = "failed"
    await session.flush()
