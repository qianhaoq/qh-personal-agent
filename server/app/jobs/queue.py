"""基于数据库 jobs 表的任务队列。

- PostgreSQL：SELECT ... FOR UPDATE SKIP LOCKED，多 worker 安全并发。
- SQLite：没有行锁，退化为“先取候选、再条件 UPDATE 抢占”的普通事务
  （单写者模型，测试与本地开发够用）。

用法：
    await enqueue(session, "organize_note", {"note_id": "..."})
    async with claim_next(session) as job:
        if job is None: ...  # 队列空
"""
import json
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import new_id
from app.models import Job
from app.timeutil import as_utc, utcnow


def is_postgres() -> bool:
    from app.config import get_settings

    return get_settings().database_url.startswith("postgresql")


async def enqueue(session: AsyncSession, kind: str, payload: dict) -> str:
    job = Job(
        id=new_id("job"),
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="pending",
    )
    session.add(job)
    await session.flush()
    return job.id


@asynccontextmanager
async def claim_next(session: AsyncSession):
    """领取一个到期的待执行 job（None 表示队列空）。

    调用方负责在同一个事务里处理并调用 mark_done / mark_failed。
    """
    now = utcnow()
    job = await _claim_postgres(session, now) if is_postgres() else await _claim_sqlite(session, now)
    yield job


async def _claim_postgres(session: AsyncSession, now) -> Job | None:
    row = (
        await session.execute(
            text(
                "SELECT id FROM jobs WHERE status = 'pending' AND run_after <= :now "
                "ORDER BY run_after, created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            ),
            {"now": now},
        )
    ).fetchone()
    if row is None:
        return None
    return await _mark_running(session, row[0], now)


async def _claim_sqlite(session: AsyncSession, now) -> Job | None:
    # 时间过滤放在 Python 里做：SQLite 读回的 naive UTC 和 aware now 不能直接比
    rows = (
        await session.execute(
            select(Job.id, Job.run_after).where(Job.status == "pending").order_by(Job.run_after, Job.created_at)
        )
    ).fetchall()
    for job_id, run_after in rows:
        if as_utc(run_after) <= now:
            return await _mark_running(session, job_id, now)
    return None


async def _mark_running(session: AsyncSession, job_id: str, now) -> Job | None:
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(status="running", attempts=Job.attempts + 1, locked_at=now)
    )
    if result.rowcount == 0:  # 已被别人抢走
        return None
    await session.flush()
    return await session.get(Job, job_id)


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
