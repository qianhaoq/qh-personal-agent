"""任务队列：回收/租约（P1-B/P2）、部分唯一索引与按 kind 的失败标记
（INV-2/INV-6，docs/server-invariants.md）。SQLite 与 PG 都跑。"""
import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs.queue import claim_next, enqueue
from app.models import Job, Note
from app.pipeline.runner import run_pending_jobs
from app.timeutil import utcnow
from tests.conftest import auth_headers, requires_postgres


async def _create_note_with_job(client, user) -> tuple[str, str]:
    """创建一条会入队 extract 任务的笔记，返回 (note_id, job_id)。"""
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "明天上午九点提醒我交周报"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]
    async with get_sessionmaker()() as session:
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalars().one()
    return note_id, job.id


async def _claim_once(job_id: str, worker_id: str = "wkr_once") -> None:
    """模拟 worker 认领后立刻崩溃：job 变 running，之后不再处理。"""
    async with get_sessionmaker()() as session, session.begin(), claim_next(session, worker_id=worker_id) as job:
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
    assert processed >= 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"
    assert job.attempts == 2  # 回收时照常 +1
    assert note.status == "organized"


async def test_expired_running_job_at_max_attempts_fails_note(client, user):
    """锁过期且次数用尽的 running 任务：不再执行，直接判 failed 并按
    kind 标记 note（extract → note.status=failed，条件入队 rev）。"""
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
    assert processed == 0  # 直接判死，没有执行任何抽取

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "failed"
    assert "过期" in (job.last_error or "")
    assert note.status == "failed"


async def test_expired_running_job_last_allowed_attempt_still_executes(client, user):
    """P2（差一回归）：max_attempts=3、attempts=2 的过期 running 任务，
    回收后的第 3 次尝试是最后一次**允许**的尝试，必须照常执行。"""
    note_id, job_id = await _create_note_with_job(client, user)
    await _claim_once(job_id)  # 第 1 次尝试，worker 崩溃（attempts=1）
    async with get_sessionmaker()() as session, session.begin():
        # 第 2 次尝试同样崩溃（attempts=2），仍 < max_attempts=3
        await session.execute(update(Job).where(Job.id == job_id).values(attempts=2))
    await _expire_lock(job_id)

    processed = await run_pending_jobs()
    assert processed >= 1  # 第 3 次（最后一次允许的）尝试被执行

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"
    assert job.attempts == 3
    assert note.status == "organized"


async def test_claim_commits_dead_job_transition_before_next_candidate(
    client, user, monkeypatch
):
    """threads11 P1 / INV-1（queue.py:182）：claim 循环判死过期 job
    （_fail_expired 锁 note + allocate_seq）后必须先提交再找下一个候选。
    钩子在领取下一个候选（置 running）的瞬间从独立会话读第一个 job：
    判死转移必须已提交可见——否则转移挂在后续领取的事务里，同一事务
    还会继续锁下一个 job/note，与并发 PATCH 构成 AB-BA（PG 真并发回归
    见 test_concurrency.py::test_claim_dead_jobs_commit_between_candidates_no_deadlock）。
    两后端都跑；调用方式按 claim_next 新契约（上下文退出后自行提交）。"""
    from app.jobs import queue as queue_mod

    headers = auth_headers(user)
    # 笔记 A 先建（其 extract job 是判死候选），笔记 B 的 job 是活候选
    note_a = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "判死转移的笔记"},
            headers=headers,
        )
    ).json()["note_id"]
    note_b = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "下一个候选的笔记"},
            headers=headers,
        )
    ).json()["note_id"]

    maker = get_sessionmaker()
    async with maker() as session:
        jobs = (
            await session.execute(select(Job).where(Job.kind == "extract"))
        ).scalars().all()
    jobs = {j.note_id: j for j in jobs}
    job_a, job_b = jobs[note_a], jobs[note_b]

    # job_a 判死（running + 锁过期 + 次数用尽）；run_after 显式错开保证
    # 扫描顺序确定（a 先于 b，不依赖 created_at 的秒级精度）
    timeout = get_settings().job_lock_timeout_seconds
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.id == job_a.id)
            .values(
                status="running",
                attempts=get_settings().job_max_attempts,
                locked_by="wkr_dead",
                locked_at=utcnow() - timedelta(seconds=timeout + 60),
                run_after=utcnow() - timedelta(hours=2),
            )
        )
        await session.execute(
            update(Job).where(Job.id == job_b.id).values(run_after=utcnow() - timedelta(hours=1))
        )

    observed = {}
    real_mark_running = queue_mod._mark_running

    async def mark_running_probe_first(session, job_id, worker_id):
        # 领取活候选（job_b）的瞬间：独立会话读判死转移是否已提交
        if job_id == job_b.id:
            async with maker() as other:
                observed["job_status"] = (await other.get(Job, job_a.id)).status
                observed["note_status"] = (await other.get(Note, note_a)).status
        return await real_mark_running(session, job_id, worker_id)

    monkeypatch.setattr(queue_mod, "_mark_running", mark_running_probe_first)

    async with maker() as session:
        async with claim_next(session, worker_id="wkr_scan11") as job:
            assert job is not None
            assert job.id == job_b.id, "判死的 job_a 不执行，领取的是活候选 job_b"
        await session.commit()

    assert observed["job_status"] == "failed", "判死转移必须在领取下一个候选之前提交"
    assert observed["note_status"] == "failed"


async def test_claim_lease_issued_at_update_moment_not_loop_start(client, user, monkeypatch):
    """threads10 / INV-13（queue.py 租约签发时刻）：locked_at 是**租约签发
    时间**，必须取置 running 语句写入时刻的 utcnow()，而不是 claim_next
    循环开头的 now——PG SELECT … FOR UPDATE 在 job 行锁上等待的时间会
    等量缩短租约（等待越久越早被回收、活 worker 越早被误判失灵）。
    钩子注入等锁窗口（置 running / 换主续租前推进可控时钟），断言
    locked_at=推进后时刻而非循环起点；回收（_reclaim_running）与首次
    领取（_mark_running）是同一断言面。"""
    from datetime import UTC, datetime

    from app.jobs import queue as queue_mod
    from app.timeutil import as_utc

    _note_id, job_id = await _create_note_with_job(client, user)

    # 循环起点放在真实入队时刻之后：run_after<=now 成立，job 可领
    loop_start = datetime.now(UTC) + timedelta(minutes=1)
    after_wait = loop_start + timedelta(seconds=30)
    clock = {"now": loop_start}
    monkeypatch.setattr(queue_mod, "utcnow", lambda: clock["now"])

    real_mark_running = queue_mod._mark_running

    async def mark_running_after_lock_wait(session, jid, worker_id):
        clock["now"] = after_wait  # FOR UPDATE 等锁窗口消耗 30s
        return await real_mark_running(session, jid, worker_id)

    monkeypatch.setattr(queue_mod, "_mark_running", mark_running_after_lock_wait)

    async with (
        get_sessionmaker()() as session,
        session.begin(),
        claim_next(session, worker_id="wkr_lease") as job,
    ):
        assert job is not None
        assert job.id == job_id, "run_after 最早的是刚创建的 extract 任务"
        assert as_utc(job.locked_at) == after_wait, "租约签发时刻=写入时刻，不被等锁时间缩短"

    # 回收路径（换主续租）同一断言面：锁过期后再次领取
    await _expire_lock(job_id)
    monkeypatch.setattr(queue_mod, "_mark_running", real_mark_running)
    after_reclaim_wait = after_wait + timedelta(minutes=2)
    real_reclaim = queue_mod._reclaim_running

    async def reclaim_after_lock_wait(session, jid, cutoff, worker_id):
        clock["now"] = after_reclaim_wait
        return await real_reclaim(session, jid, cutoff, worker_id)

    monkeypatch.setattr(queue_mod, "_reclaim_running", reclaim_after_lock_wait)

    async with (
        get_sessionmaker()() as session,
        session.begin(),
        claim_next(session, worker_id="wkr_lease2") as job,
    ):
        assert job is not None
        assert job.id == job_id
        assert as_utc(job.locked_at) == after_reclaim_wait, "换主续租的签发时刻同样是写入时刻"


# ---------------------------------------------------------------------------
# INV-6：租约换主后旧 worker 的收尾不生效（extract / memory_sync 两种 kind）
# ---------------------------------------------------------------------------


async def _hang_then_fail_llm(monkeypatch, entered: asyncio.Event, release: asyncio.Event):
    """第 1 次 LLM 调用（旧 worker）挂起后抛错；第 2 次（新 worker）正常。"""
    calls = {"llm": 0}

    class OldWorkerDiesLLM:
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

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    return calls


async def _hang_then_fail_store(monkeypatch, entered: asyncio.Event, release: asyncio.Event):
    """第 1 次 store.add（旧 worker）挂起后抛错；第 2 次（新 worker）正常。"""
    from app.memory.base import set_memory_store
    from app.memory.inmemory import InMemoryStore

    calls = {"add": 0}

    class OldWorkerDiesStore(InMemoryStore):
        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            calls["add"] += 1
            if calls["add"] == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("old worker store died")
            return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    store = OldWorkerDiesStore()
    set_memory_store(store)
    return calls


async def test_stale_worker_finalize_ignored_after_lease_reclaimed_extract(
    client, user, monkeypatch
):
    """extract：旧 worker 认领后在 LLM 里挂起 → 锁过期 → 新 worker 回收并
    完成 → 旧 worker 的 LLM 才报错返回。收尾按 (status, locked_by,
    locked_at) 条件校验：租约已是新 worker 的，旧 worker 的收尾放弃。"""
    note_id, job_id = await _create_note_with_job(client, user)

    entered, release = asyncio.Event(), asyncio.Event()
    calls = await _hang_then_fail_llm(monkeypatch, entered, release)

    old_worker = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 旧 worker 认领后在 LLM 挂起
    await _expire_lock(job_id)

    processed = await run_pending_jobs(worker_id="wkr_new")  # 回收并完成
    assert processed >= 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
    assert job.status == "done"
    assert job.attempts == 2  # 认领 +1、回收 +1
    assert job.locked_by == "wkr_new"

    release.set()
    assert await asyncio.wait_for(old_worker, timeout=5) == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"  # 未被旧 worker 改回 pending/failed
    assert job.last_error is None  # 旧 worker 的错误没有写入
    assert note.status == "organized"  # note 未被旧 worker 置 failed
    assert note.llm_calls == 1  # 只有新 worker 的那次抽取落了库
    assert calls["llm"] == 2


async def test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync(
    client, user, run_jobs, monkeypatch
):
    """memory_sync：同上，参数化的第二种 kind。旧 worker 在 store.add 里
    挂起后被回收，新 worker 完成同步；旧 worker 复活后的失败收尾被租约
    校验挡下（memory_status 不被标 failed）。"""
    note_id = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "租约换主的记忆同步"},
            headers=auth_headers(user),
        )
    ).json()["note_id"]
    await run_jobs()  # 抽取+同步完成，记忆已写

    # 再入队一个 memory_sync（模拟冗余任务），让它在 store.add 里挂起
    async with get_sessionmaker()() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = await _hang_then_fail_store(monkeypatch, entered, release)

    async with get_sessionmaker()() as session:
        job = (
            await session.execute(
                select(Job).where(
                    Job.kind == "memory_sync", Job.note_id == note_id, Job.status == "pending"
                )
            )
        ).scalar_one()
    job_id = job.id

    old_worker = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await _expire_lock(job_id)

    processed = await run_pending_jobs(worker_id="wkr_new")
    assert processed >= 1
    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
    assert job.status == "done"
    assert job.locked_by == "wkr_new"

    release.set()
    assert await asyncio.wait_for(old_worker, timeout=5) == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"
    assert job.last_error is None
    assert note.memory_status == "synced"  # 未被旧 worker 标 failed
    assert calls["add"] == 2


# ---------------------------------------------------------------------------
# INV-2：部分唯一索引与入队语义
# ---------------------------------------------------------------------------


async def test_memory_sync_single_pending_per_note(client, user):
    """INV-2.3：同 note 的 pending memory_sync 至多一个——重复入队
    （dedupe）静默跳过；连续多次 PATCH 也只剩一个 pending。"""
    async with get_sessionmaker()() as session, session.begin():
        first = await enqueue(
            session, "memory_sync", {"note_id": "nte_dedupe"}, note_id="nte_dedupe", dedupe_pending=True
        )
        again = await enqueue(
            session, "memory_sync", {"note_id": "nte_dedupe"}, note_id="nte_dedupe", dedupe_pending=True
        )
        third = await enqueue(
            session, "memory_sync", {"note_id": "nte_dedupe"}, note_id="nte_dedupe", dedupe_pending=True
        )
    assert first is not None
    assert again is None and third is None  # 被唯一索引去重
    async with get_sessionmaker()() as session:
        pendings = (
            await session.execute(
                select(Job).where(
                    Job.kind == "memory_sync", Job.note_id == "nte_dedupe", Job.status == "pending"
                )
            )
        ).scalars().all()
    assert len(pendings) == 1


async def test_memory_sync_pending_allowed_while_running(client, user):
    """INV-2.3：running 期间再入队允许新建 pending（唯一性只约束 pending 之间）。"""
    async with get_sessionmaker()() as session, session.begin():
        job_id = await enqueue(
            session, "memory_sync", {"note_id": "nte_pr"}, note_id="nte_pr", dedupe_pending=True
        )
    async with get_sessionmaker()() as session, session.begin(), claim_next(session) as job:
        assert job is not None and job.id == job_id
    async with get_sessionmaker()() as session, session.begin():
        pending2 = await enqueue(
            session, "memory_sync", {"note_id": "nte_pr"}, note_id="nte_pr", dedupe_pending=True
        )
    assert pending2 is not None  # running 不挡 pending 入队


@requires_postgres
async def test_memory_sync_single_running_per_note(client, user, run_jobs, monkeypatch):
    """INV-2.4：PG 真并发——两个 worker 独立会话同时认领同 note 的
    memory_sync，任一时刻只能有一个 running（第二个在唯一索引上让位）。

    SQLite 单写者模型下事务天然串行，构造不出该窗口；语义由
    test_memory_sync_running_unique_index_enforced 在两后端验证。
    """
    from app.memory.base import set_memory_store
    from app.memory.inmemory import InMemoryStore

    note_id = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "串行化验证"},
            headers=auth_headers(user),
        )
    ).json()["note_id"]
    await run_jobs()

    # running#1（回收窗口内）+ pending#2 并存
    async with get_sessionmaker()() as session, session.begin():
        running_id = await enqueue(
            session, "memory_sync", {"note_id": note_id}, note_id=note_id, dedupe_pending=True
        )
    async with get_sessionmaker()() as session, session.begin(), claim_next(session, worker_id="wkr_a") as job:
        assert job is not None and job.id == running_id
    async with get_sessionmaker()() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id, dedupe_pending=True)
    await _expire_lock(running_id)  # running#1 可回收

    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowStore(InMemoryStore):
        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            entered.set()
            await release.wait()
            return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    set_memory_store(SlowStore())

    async def worker(name: str) -> int:
        return await run_pending_jobs(worker_id=name)

    # 两个 worker 独立会话真并发：一个回收 running#1 进入 add，另一个尝试
    # 把 pending#2 置 running——必在唯一索引上让位
    a = asyncio.create_task(worker("wkr_a2"))
    b = asyncio.create_task(worker("wkr_b2"))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 某个 worker 已进入 add

    async with get_sessionmaker()() as session:
        running = (
            await session.execute(
                select(Job.id).where(
                    Job.kind == "memory_sync", Job.note_id == note_id, Job.status == "running"
                )
            )
        ).scalars().all()
    assert len(running) == 1, f"同 note 同时只能有一个 running memory_sync：{running}"

    release.set()
    await asyncio.wait_for(asyncio.gather(a, b), timeout=10)


async def test_memory_sync_running_unique_index_enforced(client, user):
    """INV-2.4 的 DB 层保证（两后端）：直接写第二个同 note 同状态的行
    必须违反部分唯一索引。"""
    maker = get_sessionmaker()
    for status in ("pending", "running"):
        async with maker() as session:
            session.add(Job(id=f"job_u1_{status}", kind="memory_sync", status=status, note_id="nte_u"))
            session.add(Job(id=f"job_u2_{status}", kind="memory_sync", status=status, note_id="nte_u"))
            with pytest.raises(IntegrityError):
                await session.commit()
        # extract / 其他 note 不受约束
        async with maker() as session, session.begin():
            await enqueue(session, "extract", {"note_id": "nte_u"}, note_id="nte_u", note_rev=1)
            await enqueue(session, "extract", {"note_id": "nte_u"}, note_id="nte_u", note_rev=2)
            await enqueue(
                session, "memory_sync", {"note_id": "nte_v"}, note_id="nte_v", dedupe_pending=True
            )


async def test_partial_unique_indexes_exist_both_backends(client, user):
    """INV-2 的部分唯一索引在当前后端真实存在（PG pg_indexes /
    SQLite sqlite_master）。"""
    from app.db import get_engine

    engine = get_engine()

    async def index_names(conn):
        from sqlalchemy import text

        if get_settings().database_url.startswith("postgresql"):
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'jobs'")
            )
        else:
            rows = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'")
            )
        return {r[0] for r in rows}

    async with engine.connect() as conn:
        names = await index_names(conn)
    assert "uq_jobs_memsync_pending" in names
    assert "uq_jobs_memsync_running" in names
    assert "ix_jobs_note_id" in names


async def test_extract_and_memory_sync_independent_attempts(client, user, run_jobs, monkeypatch):
    """INV-2.5：两类 job 的失败语义互不污染（修 C5）。

    - extract 耗尽 → note.status='failed'，不触碰 memory_status；
    - memory_sync 耗尽 → memory_status='failed'，不触碰 note.status。
    """
    from app.memory.base import set_memory_store
    from app.memory.inmemory import InMemoryStore

    class BoomStore(InMemoryStore):
        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            raise RuntimeError("memory backend down")

    # 方向一：extract 耗尽（note failed），memory_sync 正常完成
    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    note1 = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "抽取会失败"},
            headers=auth_headers(user),
        )
    ).json()["note_id"]
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note1)
        extract_job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note1))
        ).scalar_one()
    assert extract_job.status == "failed"
    assert note.status == "failed"
    assert note.memory_status == "pending"  # extract 失败不触碰 memory_status

    monkeypatch.undo()
    # 方向二：memory_sync 耗尽（memory_status failed），note 保持 organized
    set_memory_store(BoomStore())
    note2 = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "同步会失败"},
            headers=auth_headers(user),
        )
    ).json()["note_id"]
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note2)
        extract_job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note2))
        ).scalar_one()
        memsync_job = (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note2)
            )
        ).scalar_one()
    assert extract_job.status == "done"
    assert note.status == "organized"  # memory 失败不触碰 note.status
    assert memsync_job.status == "failed"
    assert note.memory_status == "failed"


# ---------------------------------------------------------------------------
# INV-6：失败标记的条件（按 kind 分流）
# ---------------------------------------------------------------------------


async def test_extract_finalize_marks_failed_only_at_enqueued_rev(client, user, monkeypatch):
    """INV-6：extract 失败标记的条件更新含 deleted_at IS NULL——笔记在
    耗尽前被 DELETE（最后一次失败收尾的窗口里）时，不把已删笔记标 failed。

    （DELETE 先于任务执行的情况由 test_extract_skips_when_rev_moved_marks_done
    一侧覆盖：extract 执行时读 deleted_at → 直接跳过 = done，根本不失败。）
    """
    from app.pipeline import runner
    from app.pipeline.runner import run_pending_jobs as run_jobs_runner

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    headers = auth_headers(user)
    note_id = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "收尾窗口里被删除"},
            headers=headers,
        )
    ).json()["note_id"]

    finalize_calls = {"n": 0}
    real_finalize = runner._finalize

    async def finalize_with_mid_delete(maker, claimed, error, retry_delay_seconds, worker_id):
        finalize_calls["n"] += 1
        if finalize_calls["n"] == 3:
            # 最后一次失败收尾前用户 DELETE：软删并入队 memory_sync
            deleted = await client.delete(f"/v1/notes/{note_id}", headers=headers)
            assert deleted.status_code == 204, deleted.text
        await real_finalize(maker, claimed, error, retry_delay_seconds, worker_id)

    monkeypatch.setattr(runner, "_finalize", finalize_with_mid_delete)

    # 3 次尝试全失败：extract 判 failed；失败标记的条件更新因 deleted_at
    # 非空而不命中
    processed = await run_jobs_runner(limit=3, retry_delay_seconds=0)
    assert processed == 3

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()
    assert job.status == "failed"
    assert note.deleted_at is not None
    assert note.status == "uploaded"  # 已删除的笔记不被标 failed

    # 排空剩余 job（DELETE 入队的 memory_sync）：已删笔记的记忆被清
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break


async def test_memory_sync_finalize_failure_marks_memory_status_not_note_status(
    client, user, run_jobs, monkeypatch
):
    """INV-6：memory_sync 失败收尾只置 memory_status，绝不写 note.status。"""
    from app.memory.base import set_memory_store
    from app.memory.inmemory import InMemoryStore

    class BoomStore(InMemoryStore):
        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            raise RuntimeError("memory backend down")

    set_memory_store(BoomStore())
    note_id = (
        await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "只该标 memory_status"},
            headers=auth_headers(user),
        )
    ).json()["note_id"]
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.memory_status == "failed"


# ---------------------------------------------------------------------------
# threads15 P1（runner.py:141 / extract.py）/ INV-6/13：extract 的 LLM 慢于
# lease 被回收后的迟到写回栅栏——写回事务先原子复核 (status, locked_by,
# locked_at)，与 finalize_claim 同口径。
# ---------------------------------------------------------------------------


async def _hang_then_succeed_llm(monkeypatch, entered: asyncio.Event, release: asyncio.Event):
    """第 1 次 LLM 调用（旧 worker）挂起后**返回有效结果**（慢而没死，
    threads15 的场景区别于 threads5 的「挂起后抛错」）。"""
    calls = {"llm": 0}

    class SlowButAliveLLM:
        async def chat(self, messages, response_format=None):
            calls["llm"] += 1
            if calls["llm"] == 1:
                entered.set()
                await release.wait()
            return json.dumps(
                {
                    "title": "迟到结果",
                    "tags": ["迟到"],
                    "todos": [],
                    "suggested_reminders": [
                        {"title": "迟到提醒", "fire_at": "2099-01-01T09:00:00+08:00"}
                    ],
                },
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return SlowButAliveLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    return calls


async def test_extract_stale_valid_result_fenced_after_dead_reclaim(
    client, user, monkeypatch
):
    """threads15 P1：旧 worker 认领后停在 LLM 里（租约过期）→ 回收重跑的
    次数也耗尽（判死：job failed + note.status=failed，rev 不动）→ 旧
    worker 的 LLM 才返回**有效**结果。写回事务先原子复核租约：不得把
    note 改回 organized、不建提醒、不入队 memory_sync——最终状态与
    reclaim 判死一致。"""
    from app.models import Reminder

    note_id, job_id = await _create_note_with_job(client, user)

    entered, release = asyncio.Event(), asyncio.Event()
    calls = await _hang_then_succeed_llm(monkeypatch, entered, release)

    old_worker = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 旧 worker 认领后停在 LLM

    # 等价模拟「回收重跑也一直死、次数用尽」：锁过期 + attempts 推到上限
    # （每次回收 attempts+1，直接置满避免多轮真实等待）
    await _expire_lock(job_id)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(Job).where(Job.id == job_id).values(attempts=Job.max_attempts)
        )
    processed = await run_pending_jobs(worker_id="wkr_new")  # 扫描到判死候选
    assert processed == 0
    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "failed"
    assert note.status == "failed"  # 判死标记落在入队 rev 上（rev 未动）

    release.set()  # 旧 worker 的 LLM 此刻才拿到有效结果并尝试写回
    assert await asyncio.wait_for(old_worker, timeout=5) == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
        memjobs = (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note_id)
            )
        ).scalars().all()
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
    # 迟到写回被租约栅栏挡住：note 不复活为 organized、无提醒、无记忆任务
    assert job.status == "failed", "旧 worker 不得改动已判死的 job"
    assert note.status == "failed", "旧 worker 不得把 note 写回 organized"
    assert note.llm_calls == 0, "迟到结果不得计入 llm_calls"
    assert note.rev == 1, "迟到结果不得推进 rev"
    assert memjobs == [], "迟到写回不得入队 memory_sync"
    assert reminders == [], "迟到写回不得创建 suggested reminders"
    assert calls["llm"] == 1


async def test_extract_stale_valid_result_fenced_after_new_worker_done(
    client, user, monkeypatch
):
    """threads15 P1 的对照场景：新 worker 回收后用第 2 次调用完成（note
    organized、job done）→ 旧 worker 的迟到有效结果同样不得产生第二份
    产物（llm_calls/rev 只计一次、收尾不污染新 worker 的 job 状态）。"""
    note_id, job_id = await _create_note_with_job(client, user)

    entered, release = asyncio.Event(), asyncio.Event()
    calls = await _hang_then_succeed_llm(monkeypatch, entered, release)

    old_worker = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await _expire_lock(job_id)

    processed = await run_pending_jobs(worker_id="wkr_new")  # 回收并用第 2 次调用完成
    assert processed >= 1
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.llm_calls == 1

    release.set()
    assert await asyncio.wait_for(old_worker, timeout=5) == 1

    async with get_sessionmaker()() as session:
        job = await session.get(Job, job_id)
        note = await session.get(Note, note_id)
    assert job.status == "done"  # 未被旧 worker 的收尾改动
    assert job.last_error is None
    assert note.status == "organized"
    assert note.llm_calls == 1, "迟到写回不得二次计数"
    assert note.rev == 2, "rev 只被新 worker 的 apply 推进一次"
    assert calls["llm"] == 2
