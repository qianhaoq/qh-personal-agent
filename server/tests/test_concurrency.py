"""并发安全：同 rev 并发 PATCH、同 refresh token 并发刷新、并发幂等创建。

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
# P2：organize 持锁期间不调外部服务（LLM / mem0 记忆写入）
#
# 旧实现把 LLM 调用关在 SELECT ... FOR UPDATE 事务里、把 store.add 关在
# allocate_seq 之后的大事务里：整理中的笔记 PATCH 要等 LLM 结束，全局
# sequences 行锁从分配持有到（含 mem0 内部 LLM/embedding 的）提交，所有
# 用户的写入都被串行阻塞。现在 LLM 与记忆写入都在事务之外，两个库都跑
# 这些测试：PG 验证真实行锁语义，SQLite 单写者下若仍持有事务写锁（旧
# 实现），这里的并发请求同样会因 database is locked 超时。
# ---------------------------------------------------------------------------
# P1：同一 note 上 PATCH 与 organize 写回的加锁顺序（防 AB-BA 死锁）
#
# 旧 PATCH 把 allocate_seq 写在条件更新的 .values() 里（先锁 sequences
# 再锁 note），organize 写回先 FOR UPDATE note 再 allocate_seq：两者在
# 同一 note 上重叠时 PG 报 DeadlockDetected（SQLite 无行锁、单写者串行，
# 构造不出这个环，故仅 PG 运行）。现在全仓统一为“先锁业务行（note →
# reminders），最后才 allocate_seq，change_seq 用第二条 UPDATE 写回”。
# ---------------------------------------------------------------------------


def _slow_llm(monkeypatch, entered: asyncio.Event, release: asyncio.Event) -> dict:
    """LLM 第一次调用阻塞在 release 上；返回调用计数器。"""
    import json

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

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)
    return calls


async def test_patch_during_llm_not_blocked_and_stale_result_discarded(
    client, user, monkeypatch
):
    """LLM 调用期间可以对同一笔记完成 PATCH（不被行锁阻塞）；
    之后旧任务的结果被丢弃，PATCH 重新入队的任务用新文本正常整理。"""
    from app.db import get_sessionmaker
    from app.models import Note
    from app.pipeline.runner import run_pending_jobs

    # SQLite 测试库文件跨测试复用（靠随机 ID 隔离），同文件里先前测试可能
    # 留下未执行的 pending job：先排空，保证之后进入 LLM 的是本测试的任务
    await run_pending_jobs()

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
    """整理任务在 LLM 调用与提交后记忆写入两个阶段都不持有
    sequences 行锁：别的用户创建笔记（要分配全局 seq）不被阻塞。"""
    from app.db import get_sessionmaker
    from app.memory.inmemory import InMemoryStore
    from app.models import Note
    from app.pipeline.runner import run_pending_jobs
    from tests.conftest import make_invite

    # 同上：先排空先前测试留下的 pending job
    await run_pending_jobs()

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
        """第一次 add（整理提交后的记忆写入）阻塞在 release 上。"""

        def __init__(self):
            super().__init__()
            self.add_calls = 0

        async def add(self, user_id, text, note_id=None):
            self.add_calls += 1
            if self.add_calls == 1:
                in_memory.set()
                await release_memory.wait()
            return await super().add(user_id, text, note_id=note_id)

    store = SlowStore()
    monkeypatch.setattr("app.pipeline.organize.get_memory_store", lambda: store)

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
    # 整理已提交、记忆写入（mem0 内部还有 LLM/embedding 调用）期间同样不持锁
    other_note_2 = await asyncio.wait_for(other_user_creates(), timeout=3)

    release_memory.set()
    await asyncio.wait_for(worker, timeout=10)

    # 两个用户的笔记最终都整理完成、记忆写入
    async with get_sessionmaker()() as session:
        for note_id in (user1_note_id, other_note_1, other_note_2):
            note = await session.get(Note, note_id)
            assert note.status == "organized", note_id
            assert note.memory_synced is True, note_id
    items = [m for m in await store.list(user["user_id"]) if m.note_id == user1_note_id]
    assert len(items) == 1


@requires_postgres
async def test_patch_and_organize_writeback_overlap_no_deadlock(client, user, monkeypatch):
    """P1：同一 note 上 PATCH 与 organize 写回重叠不产生 AB-BA 死锁。

    用 Event 让 organize 写回停在“已持有 note 行锁、即将请求 sequences
    锁”的位置（allocate_seq 前的业务行已全部 flush），此时发起 PATCH 并
    留出它在自己的第一把锁上阻塞的时间，再放行 organize：旧代码 PATCH
    会先抢到 sequences 锁再等 note，organize 反向等 sequences → PG
    DeadlockDetected（organize 事务被 abort、任务失败、PATCH 200 落在
    回滚后的旧 rev 上）；新代码 PATCH 只等 note 锁，organize 顺利提交，
    PATCH 复查 rev 得到确定的 409，状态一致。
    """
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Job, Note
    from app.pipeline import organize as organize_mod
    from app.pipeline.runner import run_pending_jobs

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "第一版内容"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    organize_holds_note = asyncio.Event()
    release_organize = asyncio.Event()
    real_allocate_seq = organize_mod.allocate_seq

    async def allocate_seq_paused(session, name="global", count=1):
        # 走到这里说明 organize 已 flush 完全部业务行（持有 note 行锁），
        # 正要请求 sequences 锁
        organize_holds_note.set()
        await release_organize.wait()
        return await real_allocate_seq(session, name=name, count=count)

    monkeypatch.setattr(organize_mod, "allocate_seq", allocate_seq_paused)

    worker = asyncio.create_task(run_pending_jobs())
    await asyncio.wait_for(organize_holds_note.wait(), timeout=5)

    patch_task = asyncio.create_task(
        client.patch(
            f"/v1/notes/{note_id}", json={"text": "并发写入的新内容", "rev": 1}, headers=headers
        )
    )
    await asyncio.sleep(0.3)  # 让 PATCH 走到它需要的第一把锁（note 行）上阻塞
    assert not patch_task.done(), "PATCH 应阻塞在 organize 持有的 note 行锁上"

    release_organize.set()
    patched = await asyncio.wait_for(patch_task, timeout=5)
    await asyncio.wait_for(worker, timeout=10)

    # organize 先提交（rev 1→2），PATCH 的条件更新不再命中 → 409（不是
    # DeadlockDetected 引发的 500），最终状态一致
    assert patched.status_code == 409, patched.text
    assert patched.json()["error"]["code"] == "conflict"
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalar_one()
    assert note.status == "organized"
    assert note.rev == 2
    assert note.text == "第一版内容"  # 409 的 PATCH 没有落库
    assert note.memory_synced is True
    assert job.status == "done"  # 整理任务没有被死锁打断


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
        # SQLite 测试库跨测试复用（靠随机 ID 隔离），不能数全表 user：
        # 只数本次邀请码落定的那个
        users = (
            await session.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        invite = await session.get(InviteCode, invite_code)
    assert len(users) == 1
    assert invite.used == 1  # 只计一次注册
    assert invite.owner_user_id == user_ids.pop()
