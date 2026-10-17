"""并发安全：同 rev 并发 PATCH、同 refresh token 并发刷新、并发幂等创建、
extract/PATCH/DELETE 的锁序重叠。

SQLite 是单写者模型（库级写锁），两个并发请求在数据库层面实际被串行化，
因此这些测试在 SQLite 下验证的是**条件更新的语义**——即便两个请求都读到
相同的旧状态（旧 rev / 旧 refresh token / 同 client_note_id 不存在），
也恰好只有一个请求的 `UPDATE ... WHERE 旧值`（或唯一约束）能命中，另一
个得到 409/401/已存在记录，而不是双双成功或 500。

真实的读-写竞态需要 PostgreSQL 验证：设置环境变量 TEST_DATABASE_URL
（postgresql+asyncpg://...）后 conftest 会用 PG 运行同一套测试（含本
文件）；不设置则用 SQLite。
"""
import asyncio
import json
import uuid

from tests.conftest import auth_headers, requires_postgres


async def test_concurrent_patch_same_rev_exactly_one_success(client, user):
    """同 rev 并发 PATCH：恰好一个 200、一个 409。"""
    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "并发修改前的内容"},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    note_id, rev = r.json()["note_id"], r.json()["rev"]

    async def patch(text: str):
        return await client.patch(
            f"/v1/notes/{note_id}", json={"text": text, "rev": rev}, headers=headers
        )

    results = await asyncio.gather(patch("A 改成这样"), patch("B 改成那样"))
    codes = sorted(resp.status_code for resp in results)
    assert codes == [200, 409], [resp.status_code for resp in results]

    # 落库的是成功那一个的文本，rev 恰好 +1
    got = await client.get(f"/v1/notes/{note_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["rev"] == rev + 1
    assert got.json()["text"] in ("A 改成这样", "B 改成那样")


async def test_concurrent_refresh_same_token_at_most_one_success(client, user):
    """同 refresh token 并发刷新：最多一个 200（另一个 401 复用/失效）。"""
    refresh_token = user["refresh_token"]

    async def refresh():
        return await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})

    results = await asyncio.gather(refresh(), refresh())
    codes = [resp.status_code for resp in results]
    assert codes.count(200) <= 1, codes
    assert all(code in (200, 401) for code in codes), codes


async def test_concurrent_create_same_client_note_id_idempotent(client, user):
    """并发提交相同 client_note_id：两个都 202 且指向同一条笔记，不 500。"""
    headers = auth_headers(user)
    body = {"client_note_id": str(uuid.uuid4()), "text": "并发创建同一条"}

    async def create():
        return await client.post("/v1/notes", json=body, headers=headers)

    r1, r2 = await asyncio.gather(create(), create())
    assert r1.status_code == r2.status_code == 202, (r1.text, r2.text)
    assert r1.json()["note_id"] == r2.json()["note_id"]

    listing = await client.get("/v1/notes", headers=headers)
    assert len(listing.json()["items"]) == 1


@requires_postgres
async def test_concurrent_first_allocate_seq_no_unique_violation():
    """回归：sequences 首用初始化的并发竞争不再 UniqueViolation。

    旧实现“先 SELECT 没有就 INSERT”：全新库上两个并发写事务都会 INSERT
    同一主键，后到者阻塞到先者提交后吃 UniqueViolation（上面的并发创建
    笔记在全新库上曾因此间歇 500）。现在用 ON CONFLICT DO NOTHING：
    后到者等先者的插入落定后跳过，继续走 UPDATE 拿下一个值。

    仅 PG：SQLite 单写者下两个写事务在库层面串行，没有这个窗口。
    """
    from app.common import allocate_seq
    from app.db import get_sessionmaker
    from app.models import Sequence

    maker = get_sessionmaker()
    async with maker() as sa, sa.begin():
        # 事务 A 正在初始化 sequences 行（INSERT 未提交）
        sa.add(Sequence(name="global", value=0))
        await sa.flush()

        async def b_allocates() -> int:
            async with maker() as sb, sb.begin():
                return await allocate_seq(sb)

        task = asyncio.create_task(b_allocates())
        await asyncio.sleep(0.3)
        assert not task.done(), "B 应等待 A 的未提交初始化落定，而不是越过它"
    # A 提交后：B 跳过冲突的 INSERT，走 UPDATE 拿到下一个值（不报错）
    seq_b = await asyncio.wait_for(task, timeout=5)
    assert seq_b == 1


# ---------------------------------------------------------------------------
# P2：extract 持锁期间不调外部服务（LLM / mem0 记忆写入）。
#
# 旧实现把 LLM 调用关在 SELECT ... FOR UPDATE 事务里、把 store.add 关在
# allocate_seq 之后的大事务里：抽取中的笔记 PATCH 要等 LLM 结束，全局
# sequences 行锁从分配持有到（含 mem0 内部 LLM/embedding 的）提交，所有
# 用户的写入都被串行阻塞。现在 LLM 与记忆写入都在事务之外，两个库都跑
# 这些测试：PG 验证真实行锁语义，SQLite 单写者下若仍持有事务写锁（旧
# 实现），这里的并发请求同样会因 database is locked 超时。
#
# 新模型下记忆写入是独立的 memory_sync job（事务外调 store），锁语义
# 相同：memory_sync 的外部调用阶段不持任何数据库锁/连接事务。
# ---------------------------------------------------------------------------


def _slow_llm(monkeypatch, entered: asyncio.Event, release: asyncio.Event) -> dict:
    """LLM 第一次调用阻塞在 release 上；返回调用计数器。"""
    calls = {"llm": 0}

    class SlowLLM:
        async def chat(self, messages, response_format=None):
            calls["llm"] += 1
            if calls["llm"] == 1:
                entered.set()
                await release.wait()
            return json.dumps(
                {"title": "整理结果", "tags": [], "todos": [], "suggested_reminders": []},
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return SlowLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    return calls


async def test_patch_during_llm_not_blocked_and_stale_result_discarded(
    client, user, monkeypatch
):
    """LLM 调用期间可以对同一笔记完成 PATCH（不被行锁阻塞）；
    之后旧任务的结果被丢弃，PATCH 重新入队的任务用新文本正常抽取。"""
    from app.db import get_sessionmaker
    from app.models import Note
    from app.pipeline.runner import run_pending_jobs

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "第一版内容"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    in_llm, release = asyncio.Event(), asyncio.Event()
    calls = _slow_llm(monkeypatch, in_llm, release)

    worker = asyncio.create_task(run_pending_jobs())
    await asyncio.wait_for(in_llm.wait(), timeout=5)  # 旧任务已进入 LLM 调用

    patched = await asyncio.wait_for(
        client.patch(
            f"/v1/notes/{note_id}", json={"text": "第二版内容", "rev": 1}, headers=headers
        ),
        timeout=3,
    )
    assert patched.status_code == 200, patched.text

    release.set()
    await asyncio.wait_for(worker, timeout=10)

    assert calls["llm"] == 2  # 旧任务确实在 LLM 里等待过，新任务又调了一次
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.text == "第二版内容"
    assert note.status == "organized"
    assert note.llm_calls == 1  # 旧结果在写事务复查 rev 时被丢弃，未落库


async def test_other_user_writes_during_llm_and_memory_sync(client, user, monkeypatch):
    """extract 在 LLM 调用阶段、memory_sync 在记忆写入阶段（mem0 内部
    还有 LLM/embedding）都不持有 sequences 行锁：别的用户创建笔记（要分配
    全局 seq）不被阻塞。"""
    from app.db import get_sessionmaker
    from app.memory.base import set_memory_store
    from app.memory.inmemory import InMemoryStore
    from app.models import Note
    from app.pipeline.runner import run_pending_jobs
    from tests.conftest import make_invite

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "user1 的笔记"},
        headers=headers,
    )
    user1_note_id = r.json()["note_id"]

    in_llm, release_llm = asyncio.Event(), asyncio.Event()
    in_memory, release_memory = asyncio.Event(), asyncio.Event()
    _slow_llm(monkeypatch, in_llm, release_llm)

    class SlowStore(InMemoryStore):
        """第一次 add（memory_sync job 的外部写）阻塞在 release 上。"""

        def __init__(self):
            super().__init__()
            self.add_calls = 0

        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            self.add_calls += 1
            if self.add_calls == 1:
                in_memory.set()
                await release_memory.wait()
            return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    store = SlowStore()
    set_memory_store(store)

    # 另一个用户（创建笔记要分配全局 seq）
    code = await make_invite(get_sessionmaker())
    login = await client.post(
        "/v1/auth/login", json={"invite_code": code, "device_id": "dev_conc_seq_other"}
    )
    other_headers = {"authorization": f"Bearer {login.json()['access_token']}"}

    async def other_user_creates() -> str:
        resp = await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "另一个用户的笔记"},
            headers=other_headers,
        )
        assert resp.status_code == 202, resp.text
        return resp.json()["note_id"]

    worker = asyncio.create_task(run_pending_jobs())
    await asyncio.wait_for(in_llm.wait(), timeout=5)
    # LLM 调用期间：seq 行未被持有
    other_note_1 = await asyncio.wait_for(other_user_creates(), timeout=3)

    release_llm.set()
    await asyncio.wait_for(in_memory.wait(), timeout=5)
    # 抽取已提交、memory_sync 的外部写期间同样不持锁
    other_note_2 = await asyncio.wait_for(other_user_creates(), timeout=3)

    release_memory.set()
    await asyncio.wait_for(worker, timeout=10)

    # 两个用户的笔记最终都抽取完成、记忆同步
    async with get_sessionmaker()() as session:
        for note_id in (user1_note_id, other_note_1, other_note_2):
            note = await session.get(Note, note_id)
            assert note.status == "organized", note_id
            assert note.memory_status == "synced", note_id
    items = [m for m in await store.list(user["user_id"]) if m.note_id == user1_note_id]
    assert len(items) == 1


# ---------------------------------------------------------------------------
# P1：同一 note 上 PATCH 与 extract 写回的加锁顺序（防 AB-BA 死锁）。
#
# 全局规则：先锁业务行（note → reminders），最后才 allocate_seq，
# change_seq 用第二条 UPDATE 写回。仅 PG：SQLite 无行锁、单写者串行，
# 构造不出这个环。
# ---------------------------------------------------------------------------


@requires_postgres
async def test_patch_and_organize_writeback_overlap_no_deadlock(client, user, monkeypatch):
    """P1：同一 note 上 PATCH 与 extract 写回重叠不产生 AB-BA 死锁。

    用 Event 让 extract 写回停在“已持有 note 行锁、即将请求 sequences
    锁”的位置（allocate_seq 前的业务行已全部 flush），此时发起 PATCH 并
    留出它在自己的第一把锁上阻塞的时间，再放行 extract：任何顺序违反
    （如 PATCH 先抢 sequences 再等 note）都会以 PG DeadlockDetected→500
    暴露。正确实现里 PATCH 只等 note 锁，extract 顺利提交，PATCH 复查
    rev 得到确定的 409。
    """
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Job, Note
    from app.pipeline import extract as extract_mod
    from app.pipeline.runner import run_pending_jobs

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "第一版内容"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    extract_holds_note = asyncio.Event()
    release_extract = asyncio.Event()
    real_allocate_seq = extract_mod.allocate_seq

    async def allocate_seq_paused(session, name="global", count=1):
        # 走到这里说明 extract 已 flush 完全部业务行（持有 note 行锁），
        # 正要请求 sequences 锁
        extract_holds_note.set()
        await release_extract.wait()
        return await real_allocate_seq(session, name=name, count=count)

    monkeypatch.setattr(extract_mod, "allocate_seq", allocate_seq_paused)

    worker = asyncio.create_task(run_pending_jobs(limit=1))
    await asyncio.wait_for(extract_holds_note.wait(), timeout=5)

    patch_task = asyncio.create_task(
        client.patch(
            f"/v1/notes/{note_id}", json={"text": "并发写入的新内容", "rev": 1}, headers=headers
        )
    )
    await asyncio.sleep(0.3)  # 让 PATCH 走到它需要的第一把锁（note 行）上阻塞
    assert not patch_task.done(), "PATCH 应阻塞在 extract 持有的 note 行锁上"

    release_extract.set()
    patched = await asyncio.wait_for(patch_task, timeout=5)
    await asyncio.wait_for(worker, timeout=10)
    await run_pending_jobs()  # 排空同事务入队的 memory_sync

    # extract 先提交（rev 1→2），PATCH 的条件更新不再命中 → 409（不是
    # DeadlockDetected 引发的 500），最终状态一致
    assert patched.status_code == 409, patched.text
    assert patched.json()["error"]["code"] == "conflict"
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()
    assert note.status == "organized"
    assert note.rev == 2
    assert note.text == "第一版内容"  # 409 的 PATCH 没有落库
    assert note.memory_status == "synced"
    assert job.status == "done"  # 抽取任务没有被死锁打断


# ---------------------------------------------------------------------------
# QA（PR #2 复测）：同一 note 上 PATCH、DELETE、extract 写回三方重叠。
#
# 三条路径都是先锁业务行（note 的条件 UPDATE / FOR UPDATE）→ reminders
# → jobs INSERT → 最后 allocate_seq。用 Event 让 extract 写回停在“已持有
# note 行锁、即将请求 sequences 锁”的位置，同时发出 PATCH 与 DELETE
# （各自独立会话/连接，真实并发地阻塞在 note 行锁上），重复 20 轮：PG 上
# 任何顺序违反都会以 DeadlockDetected→500 或锁等待超时暴露。仅 PG。
# ---------------------------------------------------------------------------


@requires_postgres
async def test_patch_delete_organize_three_way_overlap_no_deadlock(client, user, monkeypatch):
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.memory.base import get_memory_store
    from app.models import Job, Note, Reminder
    from app.pipeline import extract as extract_mod
    from app.pipeline.runner import run_pending_jobs

    headers = auth_headers(user)
    rounds = 20
    gate: dict[str, asyncio.Event] = {"entered": asyncio.Event(), "release": asyncio.Event()}
    real_allocate_seq = extract_mod.allocate_seq

    async def allocate_seq_paused(session, name="global", count=1):
        # 到这里说明 extract 已 flush 完业务行（持有 note 行锁），正要请求
        # sequences 锁：把三方重叠钉在这个时刻
        gate["entered"].set()
        await gate["release"].wait()
        return await real_allocate_seq(session, name=name, count=count)

    monkeypatch.setattr(extract_mod, "allocate_seq", allocate_seq_paused)

    await run_pending_jobs()  # 排空残留 job，保证每轮先被领走的是本轮任务
    note_ids: list[str] = []
    for i in range(rounds):
        gate["entered"] = asyncio.Event()
        gate["release"] = asyncio.Event()
        created = await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": f"第 {i} 轮三方重叠"},
            headers=headers,
        )
        assert created.status_code == 202, created.text
        note_id = created.json()["note_id"]
        note_ids.append(note_id)

        worker = asyncio.create_task(run_pending_jobs(limit=1))
        await asyncio.wait_for(gate["entered"].wait(), timeout=5)

        # 三者同时进入：PATCH 与 DELETE（独立请求/会话）都阻塞在 extract
        # 持有的 note 行锁上
        patch_task = asyncio.create_task(
            client.patch(
                f"/v1/notes/{note_id}",
                json={"text": "并发 PATCH 的新内容", "rev": 1},
                headers=headers,
            )
        )
        delete_task = asyncio.create_task(client.delete(f"/v1/notes/{note_id}", headers=headers))
        await asyncio.sleep(0.15)
        assert not patch_task.done() and not delete_task.done(), "两请求应在 note 行锁上等待"

        gate["release"].set()
        patched, deleted = await asyncio.gather(patch_task, delete_task)
        await asyncio.wait_for(worker, timeout=10)

        # 无 DeadlockDetected 引发的 500；结果必为合法的串行化结果之一：
        # extract 先提交（rev+1）→ PATCH 409、DELETE 204；或 DELETE 先
        # 落定 → PATCH 404、DELETE 204
        assert patched.status_code in (409, 404), (i, patched.status_code, patched.text)
        assert deleted.status_code == 204, (i, deleted.status_code, deleted.text)
        # 每轮收尾：排空本轮入队的全部 job（DELETE 的 memory_sync 等），
        # 避免下一轮的 run_pending_jobs(limit=1) 领到旧任务
        await run_pending_jobs()

    # 最终状态一致：全部已删除 → 无存活提醒、无记忆、job 全部收尾
    async with get_sessionmaker()() as session:
        notes = (
            await session.execute(select(Note).where(Note.id.in_(note_ids)))
        ).scalars().all()
        live_reminders = (
            await session.execute(
                select(Reminder).where(
                    Reminder.note_id.in_(note_ids), Reminder.deleted_at.is_(None)
                )
            )
        ).scalars().all()
        relevant_jobs = [
            j
            for j in (
                await session.execute(
                    select(Job).where(
                        Job.kind.in_(("extract", "memory_sync")), Job.note_id.in_(note_ids)
                    )
                )
            ).scalars().all()
        ]
    assert all(n.deleted_at is not None for n in notes)
    assert live_reminders == []
    assert relevant_jobs and {j.status for j in relevant_jobs} <= {"done", "failed"}
    assert [m for m in await get_memory_store().list(user["user_id"]) if m.note_id in note_ids] == []


@requires_postgres
async def test_extract_finalize_concurrent_patch_no_deadlock(client, user, monkeypatch):
    """INV-1：extract 耗尽失败收尾（jobs→note→sequences 单向）与用户
    PATCH（note→reminders→jobs→sequences）同 note 并行，不产生死锁。

    钩子把收尾停在“已锁 job 行、已条件更新 note 行、即将 allocate_seq”
    的位置，PATCH 在独立会话里同时到达：正确锁序下 PATCH 只等 note 行锁
    （收尾提交后即放行）；任何反向顺序都会以 DeadlockDetected/锁超时暴露。
    """
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.jobs import queue as queue_mod
    from app.models import Job, Note
    from app.pipeline.runner import run_pending_jobs

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "收尾与 PATCH 并发"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    entered, release = asyncio.Event(), asyncio.Event()
    real_allocate_seq = queue_mod.allocate_seq

    async def allocate_seq_paused(session, name="global", count=1):
        # 收尾已持有 job 行锁并条件更新了 note 行（rev 仍为 1），正要请求
        # sequences 锁——与 PATCH 的重叠窗口钉在这里
        entered.set()
        await release.wait()
        return await real_allocate_seq(session, name=name, count=count)

    monkeypatch.setattr(queue_mod, "allocate_seq", allocate_seq_paused)

    worker = asyncio.create_task(run_pending_jobs(retry_delay_seconds=0, worker_id="wkr_fin"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    patch_task = asyncio.create_task(
        client.patch(
            f"/v1/notes/{note_id}", json={"text": "并发 PATCH 的内容", "rev": 1}, headers=headers
        )
    )
    await asyncio.sleep(0.3)
    assert not patch_task.done(), "PATCH 应阻塞在收尾持有的 note 行锁上（而不是死锁）"

    release.set()
    patched = await asyncio.wait_for(patch_task, timeout=5)
    await asyncio.wait_for(worker, timeout=10)

    # 收尾先提交（note.status=failed，rev 仍 1）；PATCH 条件更新命中 → 200
    assert patched.status_code == 200, patched.text
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(
                select(Job).where(
                    Job.kind == "extract", Job.note_id == note_id, Job.note_rev == 1
                )
            )
        ).scalar_one()
    assert job.status == "failed"
    assert note.rev == 2
    assert note.text == "并发 PATCH 的内容"


async def test_concurrent_first_login_same_invite_single_user(client, invite_code):
    """P1：5 个并发首登同一邀请码，只创建 1 个用户。

    PG 下各请求独立会话/连接真实并发：旧“先读 owner 再创建”会让多个
    请求同时看到 owner IS NULL 而各建账号（同一码多个用户、used 乱计）。
    现在条件 UPDATE 抢占，抢不到的复查后登录先占到的人。SQLite 单写者
    下请求天然串行，本测试退化为验证同一语义（仍只 1 个用户）。
    """
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import InviteCode, User

    results = await asyncio.gather(
        *[
            client.post(
                "/v1/auth/login",
                json={
                    "invite_code": invite_code,
                    "device_id": f"dev_invite_race_{i}",
                    "device_name": "race",
                },
            )
            for i in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    user_ids = {r.json()["user_id"] for r in results}
    assert len(user_ids) == 1, f"同一邀请码不应创建多个用户：{user_ids}"

    async with get_sessionmaker()() as session:
        users = (
            await session.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        invite = await session.get(InviteCode, invite_code)
    assert len(users) == 1
    assert invite.used == 1  # 只计一次注册
    assert invite.owner_user_id == user_ids.pop()


# ---------------------------------------------------------------------------
# P2：ask 的 LLM 调用不占数据库连接（连接池压到 1 仍能服务其他请求）。
# ---------------------------------------------------------------------------


@requires_postgres
async def test_ask_slow_llm_single_connection_pool_still_serves(client, user, run_jobs, monkeypatch):
    """旧实现里鉴权依赖挂着的请求级会话在鉴权查询后就一直开着（事务未
    提交、连接未还），ask 的 LLM 秒级调用期间这唯一连接被占死，其他
    请求（含鉴权）全部排队。现在鉴权在独立短会话完成即还连接，ask
    处理中根本没有打开的会话。
    """
    import uuid as uuid_mod

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import app.db as db_module
    from app.config import get_settings

    url = get_settings().database_url
    assert url.startswith("postgresql")
    # 直接替换全局引擎（URL query 参数会被 asyncpg 当连接参数，不能走 URL）
    engine = create_async_engine(
        url, pool_pre_ping=True, future=True, pool_size=1, max_overflow=0
    )
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module,
        "_sessionmaker",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    try:
        headers = auth_headers(user)
        created = await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid_mod.uuid4()), "text": "我对花生过敏"},
            headers=headers,
        )
        assert created.status_code == 202, created.text
        await run_jobs()

        entered, release = asyncio.Event(), asyncio.Event()

        class SlowLLM:
            async def chat(self, messages, response_format=None):
                entered.set()
                await release.wait()
                return "根据你的记录：花生过敏"

        async def fake_get_llm():
            return SlowLLM()

        monkeypatch.setattr("app.api.ask.get_llm_client", fake_get_llm)

        ask_task = asyncio.create_task(
            client.post("/v1/ask", json={"text": "我吃东西要注意什么"}, headers=headers)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # 池里唯一的连接没被 ask 占住：鉴权+查询走数据库的请求照常完成
        other = await asyncio.wait_for(client.get("/v1/notes", headers=headers), timeout=3)
        assert other.status_code == 200, other.text

        release.set()
        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status_code == 200, resp.text
    finally:
        await engine.dispose()
