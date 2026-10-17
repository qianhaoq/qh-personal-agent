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


async def test_expired_running_job_last_allowed_attempt_still_executes(client, user):
    """P2（差一回归）：max_attempts=3、attempts=2 的过期 running 任务，
    回收后的第 3 次尝试是最后一次**允许**的尝试，必须照常执行。

    旧判定 attempts+1 >= max_attempts 把它直接判死，最后一次允许的
    尝试被吞掉：任务没执行就 failed、note 也被置 failed。
    """
    note_id, job_id = await _create_note_with_job(client, user)
    await _claim_once(job_id)  # 第 1 次尝试，worker 崩溃（attempts=1）
    async with get_sessionmaker()() as session, session.begin():
        # 第 2 次尝试同样崩溃（attempts=2），仍 < max_attempts=3
        await session.execute(update(Job).where(Job.id == job_id).values(attempts=2))
    await _expire_lock(job_id)

    processed = await run_pending_jobs()
    assert processed == 1  # 第 3 次（最后一次允许的）尝试被执行

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"
    assert job.attempts == 3
    assert note.status == "organized"


async def test_stale_worker_finalize_ignored_after_lease_reclaimed(client, user, monkeypatch):
    """P2（租约）：任务被回收换主后，旧 worker 的失败收尾不生效。

    旧 worker 认领后在 LLM 里挂起 → 锁过期 → 新 worker 回收并完成
    （job=done、note=organized）→ 旧 worker 的 LLM 才报错返回。收尾按
    (status, locked_by, locked_at) 条件校验：租约已是新 worker 的，
    旧 worker 的收尾放弃——job 不被改回 pending/failed，note 也不被
    置 failed、不写旧 worker 的 last_error。
    """
    import asyncio
    import json

    from app.pipeline.runner import run_pending_jobs

    await run_pending_jobs()  # 排空无关 job（SQLite 库跨测试复用）
    note_id, job_id = await _create_note_with_job(client, user)

    entered, release = asyncio.Event(), asyncio.Event()
    calls = {"llm": 0}

    class OldWorkerDiesLLM:
        """第 1 次调用（旧 worker）挂起后抛错；第 2 次（新 worker）正常。"""

        async def chat(self, messages, response_format=None):
            calls["llm"] += 1
            if calls["llm"] == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("old worker llm died")
            return json.dumps(
                {"title": "整理结果", "tags": [], "todos": [], "suggested_reminders": []},
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return OldWorkerDiesLLM()

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)

    old_worker = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 旧 worker 认领后在 LLM 挂起
    await _expire_lock(job_id)  # 锁过期：任务可被回收

    # 新 worker 回收（租约换主）并执行完成
    processed = await run_pending_jobs(worker_id="wkr_new")
    assert processed == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
    assert job.status == "done"
    assert job.attempts == 2  # 认领 +1、回收 +1
    assert job.locked_by == "wkr_new"

    # 放行旧 worker：它的 LLM 失败返回，收尾必须被租约校验挡下
    release.set()
    assert await asyncio.wait_for(old_worker, timeout=5) == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"  # 未被旧 worker 改回 pending/failed
    assert job.last_error is None  # 旧 worker 的错误没有写入
    assert note.status == "organized"  # note 未被旧 worker 置 failed
    assert note.llm_calls == 1  # 只有新 worker 的那次整理落了库
