"""任务队列：回收 running 锁过期的任务（P1-B）。SQLite 与 PG 都跑。"""
import uuid
from datetime import timedelta

from sqlalchemy import update

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs.queue import claim_next
from app.models import Job, Note
from app.pipeline.runner import run_pending_jobs
from app.timeutil import utcnow
from tests.conftest import auth_headers


async def _create_note_with_job(client, user) -> tuple[str, str]:
    """创建一条会入队整理任务的笔记，返回 (note_id, job_id)。"""
    from sqlalchemy import select

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "明天上午九点提醒我交周报"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]
    async with get_sessionmaker()() as session:
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalars().one()
    return note_id, job.id


async def _claim_once(job_id: str) -> None:
    """模拟 worker 认领后立刻崩溃：job 变 running，之后不再处理。"""
    async with get_sessionmaker()() as session, session.begin(), claim_next(session) as job:
        assert job is not None
        assert job.id == job_id


async def _expire_lock(job_id: str) -> None:
    """把 locked_at 拨回锁超时之前（免等待）。"""
    timeout = get_settings().job_lock_timeout_seconds
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(Job).where(Job.id == job_id).values(
                locked_at=utcnow() - timedelta(seconds=timeout + 60)
            )
        )


async def test_running_job_not_claimable_while_lock_fresh(client, user):
    """锁未过期的 running 任务不会被其他 worker 领走。"""
    _note_id, job_id = await _create_note_with_job(client, user)
    await _claim_once(job_id)

    processed = await run_pending_jobs()
    assert processed == 0

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
    assert job.status == "running"
    assert job.attempts == 1


async def test_expired_running_job_reclaimed_and_completes(client, user):
    """锁过期的 running 任务被回收（attempts+1）并正常完成。"""
    note_id, job_id = await _create_note_with_job(client, user)
    await _claim_once(job_id)  # 第一个“worker”领走后崩溃
    await _expire_lock(job_id)

    processed = await run_pending_jobs()
    assert processed == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"
    assert job.attempts == 2  # 回收时照常 +1
    assert note.status == "organized"


async def test_expired_running_job_at_max_attempts_fails_note(client, user):
    """锁过期且次数用尽的 running 任务：不再执行，直接判 failed 并置 note 失败。

    note 仍是入队时的 rev（没被 PATCH 过），所以条件更新命中，
    note.status=failed。
    """
    note_id, job_id = await _create_note_with_job(client, user)
    await _claim_once(job_id)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(Job).where(Job.id == job_id).values(
                attempts=get_settings().job_max_attempts
            )
        )
    await _expire_lock(job_id)

    processed = await run_pending_jobs()
    assert processed == 0  # 直接判死，没有执行任何整理

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "failed"
    assert "过期" in (job.last_error or "")
    assert note.status == "failed"
