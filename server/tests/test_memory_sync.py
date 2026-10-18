"""memory_sync 执行语义（INV-3/4/5，docs/server-invariants.md）。

覆盖：记忆归属 metadata（note_id+source_rev）、API 不直删记忆、四步
协议（写/删/复查/再入队）、C4（PATCH 后 extract 永久失败旧记忆清掉）、
C9（复查不一致只删本次 add 的 id）、删除后不复活、worker 中断回收收敛、
耗尽标 failed 的条件；4c1c9be P2-1/P2-2 的两 worker 交错（已删除路径
无条件 delete_by_note、DELETE vs 慢 add + 崩溃后不留孤儿记忆）；threads5
的租约栅栏（P2-1：复查确认持有租约，丢租约只删己 id）、枚举不完整即
失败（P1-1）、内部置位不碰 updated_at/change_seq（P2-2）。交错窗口用
DelayedAddStore 类钩子注入（真实 HTTP 请求落在钩子窗口里），不依赖
数据库层并发语义，两后端都跑。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select, update

from app.memory.base import MemoryEnumerationIncomplete, set_memory_store
from app.memory.inmemory import InMemoryStore
from app.models import Job, Note
from tests.conftest import auth_headers


async def _create_note(client, user, text: str = "我对花生过敏") -> str:
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": text},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    return r.json()["note_id"]


async def _note(note_id: str) -> Note:
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await session.get(Note, note_id)


async def _memsync_jobs(note_id: str) -> list[Job]:
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note_id)
            )
        ).scalars().all()


def get_store():
    from app.memory.base import get_memory_store

    return get_memory_store()


async def _items(store, user_id: str, note_id: str) -> list:
    return [m for m in await store.list(user_id) if m.note_id == note_id]


# ---------------------------------------------------------------------------
# INV-3 记忆归属
# ---------------------------------------------------------------------------


async def test_memory_metadata_contains_note_id_and_source_rev(client, user, run_jobs):
    note_id = await _create_note(client, user, "我妈生日是三月五号，记得准备礼物")
    await run_jobs()
    note = await _note(note_id)
    assert note.status == "organized"
    items = await _items(get_store(), user["user_id"], note_id)
    assert items, "organized 的笔记应有记忆"
    for m in items:
        assert m.note_id == note_id
        assert m.source_rev == note.rev  # 记忆只属于当前 rev


async def test_no_stale_rev_memory_after_repatch(client, user, run_jobs):
    """INV-3 / C9 端到端：PATCH 重整理后旧 rev 的记忆不再留在检索里。"""
    note_id = await _create_note(client, user, "我对花生过敏，吃饭要注意")
    await run_jobs()
    headers = auth_headers(user)
    rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "我喜欢游泳，每周去两次", "rev": rev},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    await run_jobs()

    store = get_store()
    items = await _items(store, user["user_id"], note_id)
    assert items, "新 rev 应有记忆"
    note = await _note(note_id)
    assert all(m.source_rev == note.rev for m in items)
    assert all("花生" not in m.text for m in items), "旧 rev 的记忆文本不该残留"

    # /v1/search 搜旧内容：不该命中这条笔记的记忆
    search = await client.get("/v1/search", params={"q": "花生过敏"}, headers=headers)
    assert search.status_code == 200
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []
    # 搜新内容：命中
    search2 = await client.get("/v1/search", params={"q": "游泳"}, headers=headers)
    assert [m for m in search2.json()["items"] if m["note_id"] == note_id], "新记忆应可检索"


async def test_memory_sync_fails_not_marks_synced_when_enumeration_incomplete(
    client, user, run_jobs
):
    """threads5 P1-1 / INV-3：delete_by_note 枚举不完整（一页取满删不动）→
    memory_sync 抛错让 job 失败重试至耗尽——绝不把「取不全」当成已同步
    （memory_status 落 failed，绝非 synced）。"""
    from app.pipeline.runner import run_pending_jobs

    note_id = await _create_note(client, user, "同步时枚举会不完整的笔记")

    class CappedEnumerationStore(InMemoryStore):
        """delete_by_note 见到存量 ≥ 页上限（取满且删不动）即抛
        MemoryEnumerationIncomplete，模拟超限后端。"""

        def __init__(self, cap: int = 1):
            super().__init__(list_page_size=cap)

        async def delete_by_note(self, user_id, note_id):
            stale = [
                m
                for m in self._items.values()
                if m.user_id == user_id and m.note_id == note_id
            ]
            if len(stale) >= self.list_page_size:
                raise MemoryEnumerationIncomplete(
                    f"取满一页（{len(stale)}/{self.list_page_size}）且无法推进"
                )
            return await super().delete_by_note(user_id, note_id)

    capped = CappedEnumerationStore(cap=1)
    # 预置一条存量记忆：让每次 memory_sync 的 delete_by_note 都取满页
    await capped.add(user["user_id"], "旧 rev 的存量记忆", note_id=note_id, source_rev=0)
    set_memory_store(capped)

    for _ in range(20):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break

    note = await _note(note_id)
    assert note.status == "organized", "extract 与记忆后端故障无关，照常完成"
    assert note.memory_status == "failed", "枚举不完整绝不能置 synced"
    jobs = await _memsync_jobs(note_id)
    assert jobs and {j.status for j in jobs} == {"failed"}, "任务必须失败而非报成功"
    # 存量记忆原样保留：删没删成未知，绝不假装已同步
    #（不走 _items/list——capped 的页上限对 list 同样生效）
    leftover = [m for m in capped._items.values() if m.note_id == note_id]
    assert leftover, "存量记忆必须原样保留（未被悄悄标记为已同步）"


# ---------------------------------------------------------------------------
# INV-4 API 路径不直接写删记忆
# ---------------------------------------------------------------------------


class RecordingStore(InMemoryStore):
    """记录所有写方法调用的假 store。"""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    async def add(self, user_id, text, *, note_id=None, source_rev=None):
        self.calls.append("add")
        return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    async def delete(self, user_id, memory_ids):
        self.calls.append(f"delete:{list(memory_ids)}")
        return await super().delete(user_id, memory_ids)

    async def delete_by_note(self, user_id, note_id):
        self.calls.append("delete_by_note")
        return await super().delete_by_note(user_id, note_id)

    async def delete_all(self, user_id):
        self.calls.append("delete_all")
        return await super().delete_all(user_id)


async def test_patch_and_delete_never_touch_memory_store_directly(client, user, run_jobs):
    """INV-4：HTTP PATCH/DELETE 请求期间记忆 store 零调用；排空 job 后的
    调用全部来自 memory_sync job。"""
    store = RecordingStore()
    set_memory_store(store)

    note_id = await _create_note(client, user, "先整理出一条记忆")
    await run_jobs()
    assert "add" in store.calls  # 前置：memory_sync 确实写了记忆
    store.calls.clear()

    headers = auth_headers(user)
    rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "改过的内容", "rev": rev}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert store.calls == [], f"PATCH 期间不得触碰记忆 store：{store.calls}"

    deleted = await client.delete(f"/v1/notes/{note_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert store.calls == [], f"DELETE 期间不得触碰记忆 store：{store.calls}"

    # 排空队列后：清/写只发生在 memory_sync job 里
    await run_jobs()
    assert store.calls, "排空 job 后 memory_sync 应已收敛记忆"
    jobs = await _memsync_jobs(note_id)
    assert jobs and all(j.status == "done" for j in jobs)
    assert await _items(store, user["user_id"], note_id) == []  # 终态：已删笔记无记忆


# ---------------------------------------------------------------------------
# INV-5 四步执行语义
# ---------------------------------------------------------------------------


async def test_memory_sync_writes_memory_with_source_rev_and_marks_synced(client, user, run_jobs):
    note_id = await _create_note(client, user, "周末和朋友聚餐")
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(get_store(), user["user_id"], note_id)
    assert items and all(m.source_rev == note.rev for m in items)
    jobs = await _memsync_jobs(note_id)
    assert {j.status for j in jobs} == {"done"}


async def test_memory_sync_on_deleted_note_removes_memory(client, user, run_jobs):
    note_id = await _create_note(client, user, "这条笔记会被删除")
    await run_jobs()
    store = get_store()
    assert await _items(store, user["user_id"], note_id)

    resp = await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert resp.status_code == 204
    await run_jobs()

    assert await _items(store, user["user_id"], note_id) == []
    search = await client.get(
        "/v1/search", params={"q": "这条笔记会被删除"}, headers=auth_headers(user)
    )
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []


async def test_memory_sync_deleted_path_always_calls_delete_by_note(client, user, run_jobs):
    """INV-5.2 / 4c1c9be P2-1：已删除路径必须**无条件**调 delete_by_note——
    哪怕 store 里本来就没有该 note 的记忆。两 worker 交错（旧任务的
    cleanup 先跑、慢写随后落地）叠加崩溃后，正是这条保证把残留记忆
    最终清掉；「见无记忆可删就跳过」会让孤儿记忆永久可搜。"""
    store = RecordingStore()
    set_memory_store(store)

    # 建一条笔记后立刻删除：从没跑过 job，store 里从未写过任何记忆
    note_id = await _create_note(client, user, "从未同步过就被删除的笔记")
    deleted = await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert deleted.status_code == 204, deleted.text
    assert await _items(store, user["user_id"], note_id) == []

    await run_jobs()
    assert "delete_by_note" in store.calls, "已删除路径必须调用 delete_by_note（即使无记忆可删）"
    assert "add" not in store.calls, "已删除的笔记不得再写记忆"
    jobs = await _memsync_jobs(note_id)
    assert jobs and all(j.status == "done" for j in jobs)


async def test_memory_sync_on_unorganized_note_clears_stale_memory_without_add(
    client, user, run_jobs, monkeypatch
):
    """INV-5.2 / C4：PATCH 后 extract 永久失败——旧 rev 的记忆被清掉、无新增。"""
    store = RecordingStore()
    set_memory_store(store)

    note_id = await _create_note(client, user, "旧内容：我对花生过敏")
    await run_jobs()  # organized + 记忆已写
    assert await _items(store, user["user_id"], note_id)
    added_before = store.calls.count("add")

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    headers = auth_headers(user)
    rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "新内容：我喜欢游泳", "rev": rev}, headers=headers
    )
    assert patched.status_code == 200
    # retry_delay=0 让 extract 立即重试至耗尽；memory_sync 在中途把旧记忆清掉
    from app.pipeline.runner import run_pending_jobs

    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break

    note = await _note(note_id)
    assert note.status == "failed"  # extract 耗尽
    # memory_sync 在 extract 耗尽前就已把旧记忆清空并置位（内容已收敛）；
    # 其取值为 synced（先于失败标记运行）或 failed（晚于失败标记运行），
    # 两种交错都满足「记忆 == f(note 终态) = 空」
    assert note.memory_status in ("synced", "failed")
    assert store.calls.count("add") == added_before, "未 organized 不得新增记忆"
    assert "delete_by_note" in store.calls
    assert await _items(store, user["user_id"], note_id) == [], "旧记忆必须被清掉（C4）"
    search = await client.get("/v1/search", params={"q": "花生过敏"}, headers=headers)
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []


class DelayedAddStore(InMemoryStore):
    """目标笔记的 add 先挂起等待放行：外部事件（PATCH/DELETE/别的写入）
    落在 add 窗口里，复现真实交错。

    pause_after_add=True 时再挂一次：写完记忆、复查之前停住——模拟
    「add 已落地、worker 随即崩溃」的交错（4c1c9be P2-1/P2-2 的时序）。
    """

    def __init__(self):
        super().__init__()
        self.target_note_id: str | None = None
        self.target_source_rev: int | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause_after_add = False
        self.wrote = asyncio.Event()
        self.resume_after_add = asyncio.Event()
        self.in_window: list[str] = []
        self.added_in_window: list = []
        self.deleted_ids: list[str] = []

    def _is_target(self, note_id, source_rev) -> bool:
        return note_id == self.target_note_id and (
            self.target_source_rev is None or source_rev == self.target_source_rev
        )

    async def add(self, user_id, text, *, note_id=None, source_rev=None):
        if self._is_target(note_id, source_rev) and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
            self.in_window.append("add")
            items = await super().add(user_id, text, note_id=note_id, source_rev=source_rev)
            self.added_in_window.extend(items)
            if self.pause_after_add:
                self.wrote.set()
                await self.resume_after_add.wait()
            return items
        return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    async def delete(self, user_id, memory_ids):
        self.deleted_ids.append(list(memory_ids))
        return await super().delete(user_id, memory_ids)

    async def delete_by_note(self, user_id, note_id):
        # InMemoryStore.delete_by_note 直改内部 dict、不经过 delete()：
        # 这里记下它实际删掉的 id，供「清掉的方式是 delete_by_note」断言
        ids = [
            m.id
            for m in self._items.values()
            if m.user_id == user_id and m.note_id == note_id
        ]
        removed = await super().delete_by_note(user_id, note_id)
        if ids:
            self.deleted_ids.append(ids)
        return removed


async def test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change(
    client, user, run_jobs
):
    """INV-5.3 / C9：add 窗口内笔记被 PATCH（rev 变化）——复查发现后只删
    本次 add 产生的 id，绝不 delete_by_note 误删并发新 rev 刚写的记忆；
    并再入队（与 PATCH 已入队的 pending 去重合并）。"""
    note_id = await _create_note(client, user, "版本二：我对花生过敏")
    await run_jobs()  # organized rev=2，记忆 source_rev=2

    store = DelayedAddStore()
    store.target_note_id = note_id
    store.target_source_rev = 2  # 只有旧 rev 的 add 挂起
    set_memory_store(store)

    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs

    # 手动再入队一个 memory_sync（模拟去重竞争中留下的冗余任务）
    async with get_sessionmaker()() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    worker = asyncio.create_task(run_pending_jobs(limit=1))
    await asyncio.wait_for(store.entered.wait(), timeout=5)  # 旧任务 add 挂起中

    headers = auth_headers(user)
    # 窗口内：并发的“新 rev 任务”刚写了一条记忆（模拟另一 worker 的写入）
    foreign = await store.add(
        user["user_id"], "并发新任务刚写入的记忆", note_id=note_id, source_rev=99
    )
    # 窗口内：用户 PATCH（rev 2→3，enqueue extract(rev=3) + memory_sync pending）
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "版本三：我喜欢游泳", "rev": 2}, headers=headers
    )
    assert patched.status_code == 200, patched.text

    store.release.set()
    await asyncio.wait_for(worker, timeout=10)

    # 旧任务收尾：只删自己 add 的 id（C9），别的写入原样保留
    own_ids = [m.id for m in store.added_in_window]
    assert own_ids, "前置失败：旧任务应在窗口内 add 过记忆"
    assert store.deleted_ids == [own_ids], (
        f"复查不一致时必须只删本次 add 的 id：{store.deleted_ids}"
    )
    assert [m for m in await store.list(user["user_id"]) if m.id == foreign[0].id], (
        "并发新写入的记忆不得被旧任务误删"
    )
    # 慢 add 收尾后新写入立刻可检索（4c1c9be P2-2：复查清理绝不清掉它）
    mid = await client.get("/v1/search", params={"q": "并发新任务"}, headers=headers)
    assert mid.status_code == 200
    assert [m for m in mid.json()["items"] if m["memory_id"] == foreign[0].id], (
        "并发新写入的记忆在旧任务收尾后必须仍可检索"
    )
    # 再入队发生（与 PATCH 的 pending 去重合并后仍有一个 pending 在队列里）
    note = await _note(note_id)
    assert note.rev == 3

    # 排空：按新状态收敛（extract rev=3 → organized rev=4 → 记忆=新文本）
    await run_jobs()
    note = await _note(note_id)
    assert note.status == "organized"
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all("游泳" in m.text for m in items)
    assert all(m.source_rev == note.rev for m in items)
    assert [m for m in items if m.id == foreign[0].id] == [], "旧杂散记忆最终被收敛清掉"
    final = await client.get("/v1/search", params={"q": "游泳"}, headers=headers)
    assert [m for m in final.json()["items"] if m["note_id"] == note_id], (
        "新 rev 的记忆最终必须可检索"
    )


async def test_memory_sync_converges_after_delete_during_add(client, user, run_jobs):
    """INV-5：删除落在 add 窗口内——复查发现 deleted，删掉本次新增并再入队，
    最终该笔记的记忆彻底消失（不复活）。"""
    note_id = await _create_note(client, user, "这条笔记的记忆会在写入途中被删除")
    await run_jobs()

    store = DelayedAddStore()
    store.target_note_id = note_id
    set_memory_store(store)

    # 手动再入队一个 memory_sync，让它在 add 里挂起
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs

    async with get_sessionmaker()() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    worker = asyncio.create_task(run_pending_jobs(limit=1))
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # 窗口内：真实 DELETE 请求（enqueue memory_sync、软删、提交）
    deleted = await asyncio.wait_for(
        client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user)), timeout=3
    )
    assert deleted.status_code == 204, deleted.text

    store.release.set()
    await asyncio.wait_for(worker, timeout=10)

    # 旧任务只删自己 add 的 id；DELETE 入队的任务随后把一切清干净
    own_ids = [m.id for m in store.added_in_window]
    assert own_ids and store.deleted_ids == [own_ids]
    await run_jobs()
    assert await _items(store, user["user_id"], note_id) == []
    search = await client.get(
        "/v1/search", params={"q": "写入途中被删除"}, headers=auth_headers(user)
    )
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []
    ask = await client.post(
        "/v1/ask", json={"text": "被删除的笔记说了什么"}, headers=auth_headers(user)
    )
    assert ask.status_code == 200
    assert "写入途中被删除" not in ask.json()["answer"]


async def test_two_worker_delete_vs_add_converges_no_orphan(client, user, run_jobs):
    """INV-5 / 4c1c9be P2-1 的两 worker 交错：旧任务的 cleanup
    （delete_by_note）先跑完 → 慢 add 随后落地（记忆重新出现）→ 旧 worker
    在复查前崩溃（job 卡 running）。旧模型里 organize_note 重跑见已删除
    就 return 不删记忆 → 永久可搜；新模型里回收重跑的 memory_sync 走
    「已删除」路径无条件 delete_by_note → 孤儿被清、最终搜不到。"""
    note_id = await _create_note(client, user, "两个 worker 交错时会被写出的记忆")
    await run_jobs()

    store = DelayedAddStore()
    store.target_note_id = note_id
    store.pause_after_add = True  # 写完记忆、复查前停住（崩溃窗口）
    set_memory_store(store)

    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    # 冗余的旧 memory_sync（模拟 ≥2 worker 时留下的重复任务）
    async with get_sessionmaker()() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    worker1 = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(store.entered.wait(), timeout=5)  # add 挂起中

    # 放行 add：cleanup（delete_by_note）已跑完，新记忆落地后停在写后钩子
    store.release.set()
    await asyncio.wait_for(store.wrote.wait(), timeout=5)

    # 紧随其后的 DELETE（另一请求路径）：软删 + enqueue memory_sync
    deleted = await asyncio.wait_for(
        client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user)), timeout=3
    )
    assert deleted.status_code == 204, deleted.text

    # 旧 worker 复查前崩溃：job 卡 running，刚写出的记忆成为潜在孤儿
    worker1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker1, timeout=5)
    orphan_ids = [m.id for m in store.added_in_window]
    assert orphan_ids, "前置失败：崩溃前 add 的记忆应已落地"
    assert [m for m in await store.list(user["user_id"]) if m.id in orphan_ids]

    # 租约过期 → 另一 worker 回收重跑：deleted 路径 delete_by_note 清掉孤儿
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(
                Job.kind == "memory_sync",
                Job.note_id == note_id,
                Job.status == "running",
            )
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )

    await run_jobs()

    assert await _items(store, user["user_id"], note_id) == [], "孤儿记忆必须被回收重跑清掉"
    flat_deleted = [mid for ids in store.deleted_ids for mid in ids]
    assert set(orphan_ids) <= set(flat_deleted), "清掉的方式是 delete_by_note（按 id）"
    jobs = await _memsync_jobs(note_id)
    assert jobs and all(j.status == "done" for j in jobs)
    search = await client.get(
        "/v1/search", params={"q": "两个 worker 交错"}, headers=auth_headers(user)
    )
    assert search.status_code == 200
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []
    ask = await client.post(
        "/v1/ask", json={"text": "被删除的笔记写了什么"}, headers=auth_headers(user)
    )
    assert ask.status_code == 200
    assert "两个 worker 交错" not in ask.json()["answer"]


async def test_memory_sync_worker_killed_mid_run_reclaimed_converges(client, user, run_jobs):
    """INV-5：worker 在外部写中途被杀（job 卡 running）→ 租约过期回收重跑，
    按 note 当前状态收敛。"""
    note_id = await _create_note(client, user, "回收前的内容")
    await run_jobs()

    headers = auth_headers(user)
    rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "回收后的新内容", "rev": rev}, headers=headers
    )
    assert patched.status_code == 200

    # 先让 extract（PATCH 入队、排在 memory_sync 前面）正常完成
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import claim_next
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    maker = get_sessionmaker()
    assert await run_pending_jobs(limit=1) == 1

    # 模拟 worker 认领 memory_sync 后立刻崩溃（job 留在 running）
    from sqlalchemy import update

    memsync_id = None
    async with maker() as session, session.begin(), claim_next(session, worker_id="wkr_dead") as job:
        assert job is not None
        assert job.kind == "memory_sync"
        memsync_id = job.id
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.id == memsync_id)
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )

    processed = await run_pending_jobs()  # 回收 + 重跑 + 排空其余 job
    assert processed >= 1

    note = await _note(note_id)
    assert note.status == "organized"
    assert note.memory_status == "synced"
    items = await _items(get_store(), user["user_id"], note_id)
    assert items and all("回收后的新内容" == m.text for m in items)
    async with maker() as session:
        job = await session.get(Job, memsync_id)
    assert job.status == "done"
    assert job.attempts == 2  # 认领 +1、回收 +1


async def test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job(
    client, user, run_jobs, monkeypatch
):
    """INV-2.5/INV-5：memory_sync 耗尽 → memory_status='failed'，但**仅当**
    没有更新的 pending/running memory_sync（失败只是中间态时不落 failed）。"""
    note_id = await _create_note(client, user, "同步会失败的内容")
    await run_jobs()

    headers = auth_headers(user)
    rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]

    calls = {"add": 0}

    class FlakyStore(InMemoryStore):
        """add 可开关地抛错：模拟记忆后端故障与恢复。"""

        def __init__(self):
            super().__init__()
            self.broken = True

        async def add(self, user_id, text, *, note_id=None, source_rev=None):
            calls["add"] += 1
            if self.broken:
                raise RuntimeError("memory backend down")
            return await super().add(user_id, text, note_id=note_id, source_rev=source_rev)

    flaky = FlakyStore()
    set_memory_store(flaky)
    # PATCH 触发一次 memory_sync（extract 正常，store.add 全挂）
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "改过但同步失败", "rev": rev}, headers=headers
    )
    assert patched.status_code == 200

    from app.pipeline import runner
    from app.pipeline.runner import run_pending_jobs

    finalize_calls = {"n": 0}
    real_finalize = runner._finalize

    async def finalize_with_mid_race_patch(maker, claimed, error, retry_delay_seconds, worker_id):
        finalize_calls["n"] += 1
        if finalize_calls["n"] == 3 and claimed.kind == "memory_sync":
            # 旧 memory_sync 第 2 次失败收尾前，用户再 PATCH：留下新的
            # pending memory_sync（失败只是中间态，不落 failed；旧任务回
            # pending 会撞唯一索引，让位标 done）
            cur = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
            again = await client.patch(
                f"/v1/notes/{note_id}", json={"text": "又一次修改", "rev": cur}, headers=headers
            )
            assert again.status_code == 200, again.text
            flaky.broken = False  # 后续同步恢复
        await real_finalize(maker, claimed, error, retry_delay_seconds, worker_id)

    monkeypatch.setattr(runner, "_finalize", finalize_with_mid_race_patch)
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break

    note = await _note(note_id)
    assert note.memory_status != "failed", "还有更新的任务在途时不得落 failed"

    # 排空：最终按最新状态收敛为 synced
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break
    note = await _note(note_id)
    assert note.memory_status == "synced"
    assert note.status == "organized"

    # 反向场景：没有更新的任务时，耗尽就落 failed（且不动 note.status）
    flaky2 = FlakyStore()
    set_memory_store(flaky2)
    cur = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
    patched2 = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "彻底同步失败", "rev": cur}, headers=headers
    )
    assert patched2.status_code == 200
    monkeypatch.undo()
    for _ in range(10):
        if await run_pending_jobs(retry_delay_seconds=0) == 0:
            break
    note = await _note(note_id)
    assert note.memory_status == "failed"
    assert note.status == "organized"  # memory 失败不污染 note.status（修 C5）


async def test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids(
    client, user, run_jobs
):
    """threads5 P2-1 / INV-5/INV-6：add 挂起期间租约过期被回收换主、新持有者
    已把 job 跑完 → 复活的慢 worker 复查发现租约已丢：只删自己刚写的 id、
    不置位（threads6 P1-1 起：另留下一个补偿任务，排空后终态仍恰好一组
    记忆——迟到 worker 不得多写一组，也不得让损害无人兜底）。"""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    note_id = await _create_note(client, user, "租约被回收后迟到的写入")
    await run_jobs()  # organized rev=2；初次收敛已完成

    store = DelayedAddStore()
    store.target_note_id = note_id
    set_memory_store(store)

    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    # 慢 worker 认领后在 add 里挂起（外部写超过锁超时的窗口）
    worker1 = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # 租约过期 → 新 worker 回收换主并把同一 job 跑完（写下「正主」一组记忆）
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.kind == "memory_sync", Job.note_id == note_id, Job.status == "running")
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )
    assert await run_pending_jobs(limit=1, worker_id="wkr_new") == 1
    winner_ids = [m.id for m in await store.list(user["user_id"])]
    assert winner_ids, "前置失败：新持有者应已写下一组记忆"
    note = await _note(note_id)
    assert note.memory_status == "synced"

    # 复查前把 memory_status 复位（模拟期间一次不动 rev 的内部推进）：若慢
    # worker 的复查只比 (rev, deleted_at)，它会再次把这里覆盖成 synced
    async with maker() as session, session.begin():
        await session.execute(update(Note).where(Note.id == note_id).values(memory_status="pending"))

    store.release.set()
    await asyncio.wait_for(worker1, timeout=10)

    own_ids = [m.id for m in store.added_in_window]
    assert own_ids and not set(own_ids) & set(winner_ids)
    assert store.deleted_ids == [own_ids], "租约已丢：只删自己刚写的 id，别的不碰"
    note = await _note(note_id)
    assert note.memory_status == "pending", "丢租约的执行不得置位"
    # 正主的那组原样保留，迟到的写入被自己清掉
    assert [m.id for m in await store.list(user["user_id"])] == winner_ids
    # threads6 P1-1：丢租约的执行必须留下一个补偿任务（此刻损害尚未发生，
    # 但迟到删除随时可能落地——没有它就再无后继收敛）
    jobs = await _memsync_jobs(note_id)
    compensating = [j for j in jobs if j.status == "pending"]
    assert len(compensating) == 1, f"应恰有一个补偿 pending：{[j.status for j in jobs]}"

    # 排空：补偿任务按 note 当前状态幂等收敛，终态恰好一组记忆
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all(m.source_rev == note.rev for m in items)
    assert len({m.id for m in items}) == len(items)
    jobs = await _memsync_jobs(note_id)
    assert not [j for j in jobs if j.status in ("pending", "running")]


async def test_memory_sync_mark_status_skipped_when_lease_lost_before_update(
    client, user, run_jobs, monkeypatch
):
    """threads8 P2 / INV-5/6/13（memory_sync.py:131）：复查会话看到租约
    仍在、状态未变，但置位事务开启前协程暂停、租约被回收换主（job 回
    pending 等待新认领）→ 置位必须与租约复核**同事务原子**：迟到的
    worker 不得写下 synced 掩盖「换主后尚未收敛」的状态，静默让位给
    后继任务；排空后由新持有者正常置位。"""
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline import memory_sync
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    note_id = await _create_note(client, user, "置位前一刻丢租约的笔记")
    await run_jobs()  # organized + 初次收敛（memory_status=synced）

    # 复位 memory_status：若置位没被租约栅栏拦下，迟到的 worker 会把它
    # 覆盖成 synced（模拟期间一次不动 rev 的内部推进，同上例手法）
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await session.execute(
            update(Note).where(Note.id == note_id).values(memory_status="pending")
        )
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    real_mark = memory_sync._mark_memory_status

    async def mark_after_lease_reclaim(maker_, note_id_, rev_, *, failed, **lease):
        # 注入「复查通过 → 置位事务开启」之间的暂停窗口：回收者把租约
        # 换主（status 回 pending；真实并发发生在别的连接上，等价）
        async with maker() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == lease["job_id"])
                .values(
                    status="pending", run_after=utcnow(), locked_by=None, locked_at=None
                )
            )
        return await real_mark(maker_, note_id_, rev_, failed=failed, **lease)

    monkeypatch.setattr(memory_sync, "_mark_memory_status", mark_after_lease_reclaim)
    assert await run_pending_jobs(limit=1, worker_id="wkr_stale") == 1

    note = await _note(note_id)
    assert note.memory_status == "pending", "丢租约的迟到置位必须被拦下"
    jobs = await _memsync_jobs(note_id)
    running_or_pending = [j.status for j in jobs if j.status in ("pending", "running")]
    assert running_or_pending == ["pending"], (
        f"被回收的任务应回到 pending 等后继：{[j.status for j in jobs]}"
    )

    # 排空：换主后的新持有者按当前状态正常置位
    monkeypatch.undo()
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"


class StalledDeleteStore(InMemoryStore):
    """目标笔记的**第一次** delete_by_note 先挂起：模拟 mem0 清理调用本身
    耗过锁超时（threads6 P1-1 的交错源头），放行后照常执行删除。"""

    def __init__(self):
        super().__init__()
        self.target_note_id: str | None = None
        self.first_delete_entered = asyncio.Event()
        self.release_first_delete = asyncio.Event()

    async def delete_by_note(self, user_id, note_id):
        if note_id == self.target_note_id and not self.first_delete_entered.is_set():
            self.first_delete_entered.set()
            await self.release_first_delete.wait()
        return await super().delete_by_note(user_id, note_id)


async def test_memory_sync_stalled_delete_by_note_past_lease_requeues_and_converges(
    client, user, run_jobs
):
    """threads6 P1-1 / INV-5：旧 worker 停在 delete_by_note 里超过锁超时 →
    租约被回收、新 worker 把 job 跑完（写记忆、置 synced）→ 旧 worker 的
    删除此刻才落地，把正主刚写的记忆删掉 → 复查丢租约：删己 id + 补偿
    再入队 → 排空后收敛回「笔记仍有记忆、/v1/search 可搜到」。没有补偿
    再入队时，终态是 note 永久无记忆、memory_status=synced、队列空。"""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    note_id = await _create_note(client, user, "删除挂起超过租约的笔记")
    await run_jobs()  # organized rev=2，初次收敛完成

    store = StalledDeleteStore()
    store.target_note_id = note_id
    set_memory_store(store)

    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    # 旧 worker 认领后在 delete_by_note 里挂起（外部清理超过锁超时的窗口）
    worker1 = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(store.first_delete_entered.wait(), timeout=5)

    # 租约过期 → 新 worker 回收换主并把同一 job 跑完（写下一组记忆、置 synced）
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.kind == "memory_sync", Job.note_id == note_id, Job.status == "running")
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )
    assert await run_pending_jobs(limit=1, worker_id="wkr_new") == 1
    winner_ids = [m.id for m in await store.list(user["user_id"])]
    assert winner_ids, "前置失败：新持有者应已写下一组记忆"
    note = await _note(note_id)
    assert note.memory_status == "synced"

    # 旧 worker 的删除此刻才落地：删掉的是正主刚写的记忆（损害发生）
    store.release_first_delete.set()
    await asyncio.wait_for(worker1, timeout=10)

    # 损害确实发生且未静默终局：store 里该笔记已无记忆，但队列里留下了
    # 恰好一个补偿 pending（旧行为在此终结：无 pending + synced + 空记忆）
    assert await _items(store, user["user_id"], note_id) == [], (
        "前置失败：迟到的 delete_by_note 应已把正主的记忆删掉"
    )
    jobs = await _memsync_jobs(note_id)
    compensating = [j for j in jobs if j.status == "pending"]
    assert len(compensating) == 1, f"丢租约后必须补偿再入队：{[j.status for j in jobs]}"
    note = await _note(note_id)
    assert note.memory_status == "synced", "丢租约的执行不得改写 memory_status"

    # 排空：补偿任务收敛回「记忆 == f(note 终态)」
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all("删除挂起超过租约" in m.text for m in items)
    assert all(m.source_rev == note.rev for m in items)
    search = await client.get(
        "/v1/search", params={"q": "删除挂起超过租约"}, headers=auth_headers(user)
    )
    assert search.status_code == 200
    assert [m for m in search.json()["items"] if m["note_id"] == note_id], (
        "补偿收敛后该笔记的记忆必须重新可检索"
    )
    jobs = await _memsync_jobs(note_id)
    assert not [j for j in jobs if j.status in ("pending", "running")]


async def test_memory_sync_status_updates_do_not_touch_updated_at_or_change_seq(
    client, user, run_jobs
):
    """threads5 P2-2 / INV-2.6：memory_status 是内部字段（不进 API 响应）——
    重放 memory_sync 前后 note 的 updated_at 与 change_seq 都不动，
    /v1/sync 按 cursor 也拉不到这条 note。"""
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue

    note_id = await _create_note(client, user, "内部状态更新不应产生可观察时间戳")
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"

    headers = auth_headers(user)
    before_seq, before_updated = note.change_seq, note.updated_at
    page = await client.get("/v1/sync", params={"since": before_seq}, headers=headers)
    assert [n for n in page.json()["notes"] if n["note_id"] == note_id] == []

    # 冗余重放一次 memory_sync（置位路径再次执行，值不变）
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)
    await run_jobs()

    after = await _note(note_id)
    assert after.memory_status == "synced", "重放照常置位（值不变）"
    assert after.change_seq == before_seq, "内部置位不得分配 change_seq"
    assert after.updated_at == before_updated, "内部置位不得推进 updated_at"

    page2 = await client.get("/v1/sync", params={"since": before_seq}, headers=headers)
    assert [n for n in page2.json()["notes"] if n["note_id"] == note_id] == [], (
        "updated_at 若被推进而 seq 不动，客户端永远观察不到那个新时间戳"
    )


async def test_inmemory_list_page_cap_raises_when_full():
    """INV-3（后端契约第 1 条）：InMemoryStore 的 list() 页上限取满即抛
    MemoryEnumerationIncomplete（进程内字典本不截断，上限让"取不全即报错"
    的契约可测；mem0 后端实现同一语义）。"""
    capped = InMemoryStore(list_page_size=2)
    for text, note in (("甲", "n1"), ("乙", "n1"), ("丙", "n2")):
        await capped.add("usr_1", text, note_id=note)
    with pytest.raises(MemoryEnumerationIncomplete):
        await capped.list("usr_1")

    # 上限高于存量：可证明取全，照常返回
    roomy = InMemoryStore(list_page_size=5)
    await roomy.add("usr_1", "甲", note_id="n1")
    assert len(await roomy.list("usr_1")) == 1



class FailingCleanupStore(DelayedAddStore):
    """删己 id 的清理调用（delete）记录后无条件抛异常：模拟记忆后端在
    复查清理阶段故障——threads7 P1 / INV-5 的 try/finally 触发点。"""

    async def delete(self, user_id, memory_ids):
        self.deleted_ids.append(list(memory_ids))
        raise RuntimeError("cleanup exploded")


async def test_memory_sync_lost_lease_delete_failure_still_requeues(
    client, user, run_jobs
):
    """threads7 P1 / INV-5（memory_sync.py:123）：丢租约路径的「删己 id →
    补偿再入队」必须 try/finally——清理调用抛异常也一定留下补偿任务。
    本执行租约已丢、收尾会被 finalize_claim 放弃（无重试），入队若被
    吞掉就再无任何收敛途径。"""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import utcnow

    note_id = await _create_note(client, user, "清理会在丢租约后炸掉的笔记")
    await run_jobs()  # organized rev=2，初次收敛完成

    store = FailingCleanupStore()
    store.target_note_id = note_id
    set_memory_store(store)

    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)

    worker1 = asyncio.create_task(run_pending_jobs(limit=1, worker_id="wkr_old"))
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # 租约过期 → 新 worker 回收换主并把同一 job 跑完（置 synced）
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.kind == "memory_sync", Job.note_id == note_id, Job.status == "running")
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )
    assert await run_pending_jobs(limit=1, worker_id="wkr_new") == 1

    store.release.set()
    await asyncio.wait_for(worker1, timeout=10)

    # 复活的慢 worker：复查丢租约 → 删己 id 抛异常，但补偿任务必须已在
    assert store.deleted_ids, "前置失败：慢 worker 应尝试过删己 id"
    jobs = await _memsync_jobs(note_id)
    compensating = [j for j in jobs if j.status == "pending"]
    assert len(compensating) == 1, (
        f"清理抛异常也必须留下补偿 pending：{[j.status for j in jobs]}"
    )

    # 排空后照常收敛：补偿任务 delete_by_note 重做清理、写新记忆、置 synced
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all(m.source_rev == note.rev for m in items)
    assert len({m.id for m in items}) == len(items), "终态恰好一组记忆"


async def test_memory_sync_rev_change_delete_failure_still_requeues(
    client, user, run_jobs
):
    """threads7 P1 / INV-5（memory_sync.py:123）：状态已变路径同样要求
    「删己 id → 补偿再入队」在 try/finally 里——清理抛异常时异常上抛，
    而补偿任务不缺席。

    直接调 sync_note_memory（不走 runner，无重试兜底）并让窗口内的状态
    变化**不留自己的 pending**（直接推进 rev，模拟变更事务的后继已被
    消费/让位的交错）：此刻补偿入队是唯一可能的后继任务——吞掉它，
    这条笔记就永远无人收敛。"""
    from app.db import get_sessionmaker
    from app.pipeline.memory_sync import sync_note_memory

    note_id = await _create_note(client, user, "清理会在复查后炸掉的笔记")
    await run_jobs()  # organized rev=2

    store = FailingCleanupStore()
    store.target_note_id = note_id
    set_memory_store(store)

    maker = get_sessionmaker()
    task = asyncio.create_task(sync_note_memory(note_id))
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # add 窗口内笔记 rev 变化，且不产生别的 pending
    async with maker() as session, session.begin():
        await session.execute(update(Note).where(Note.id == note_id).values(rev=3))

    store.release.set()
    # 异常照常上抛（调用方决定重试），但 finally 已完成补偿入队
    with pytest.raises(RuntimeError, match="cleanup exploded"):
        await asyncio.wait_for(task, timeout=10)

    assert store.deleted_ids, "前置失败：本次执行应尝试过删己 id"
    jobs = await _memsync_jobs(note_id)
    pending = [j for j in jobs if j.status == "pending"]
    assert len(pending) == 1, (
        f"清理抛异常也必须留下后继 pending：{[j.status for j in jobs]}"
    )

    # 排空：补偿任务按新 rev 收敛，终态 synced
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all(m.source_rev == note.rev for m in items)


class FailingAfterWriteStore(DelayedAddStore):
    """add 写入成功后再抛异常：模拟 mem0 已写 PG vector、随后 history.db
    /网络失败等「部分成功」场景（P1-4 / INV-5）。"""

    def __init__(self):
        super().__init__()
        self.exploded = False

    async def add(self, user_id, text, *, note_id=None, source_rev=None):
        if self._is_target(note_id, source_rev) and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
            self.in_window.append("add")
            items = await InMemoryStore.add(
                self, user_id, text, note_id=note_id, source_rev=source_rev
            )
            self.added_in_window.extend(items)
            self.exploded = True
            raise RuntimeError("add exploded")
        return await InMemoryStore.add(
            self, user_id, text, note_id=note_id, source_rev=source_rev
        )


async def test_memory_sync_add_failure_after_lost_lease_requeues_and_raises(
    client, user, run_jobs
):
    """P1-4 / INV-5：外部写（add）部分成功后抛异常时，仍须检查租约；
    租约已丢则补偿 enqueue 再 re-raise——否则 finalize_claim 拒旧租约且
    不排队，留下陈旧/重复记忆。"""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.jobs.queue import claim_next, enqueue
    from app.pipeline.memory_sync import sync_note_memory
    from app.pipeline.runner import run_pending_jobs
    from app.timeutil import as_utc, utcnow

    note_id = await _create_note(client, user, "add 会在写后炸掉的笔记")
    await run_jobs()  # organized，初次收敛完成

    store = FailingAfterWriteStore()
    store.target_note_id = note_id
    set_memory_store(store)

    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await enqueue(session, "memory_sync", {"note_id": note_id}, note_id=note_id)
    async with maker() as session, session.begin(), claim_next(
        session, worker_id="wkr_old"
    ) as job:
        assert job is not None and job.kind == "memory_sync"
        job_id, locked_at = job.id, as_utc(job.locked_at)

    task = asyncio.create_task(
        sync_note_memory(
            note_id, job_id=job_id, worker_id="wkr_old", locked_at=locked_at
        )
    )
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # 租约过期 → 新 worker 回收换主把同一 job 跑完（正主记忆 + synced）
    async with maker() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                locked_at=utcnow()
                - timedelta(seconds=get_settings().job_lock_timeout_seconds + 60)
            )
        )
    assert await run_pending_jobs(limit=1, worker_id="wkr_new") == 1
    winner_ids = [m.id for m in await store.list(user["user_id"])]
    assert winner_ids, "前置失败：新持有者应已写下一组记忆"

    # 慢 worker 的 add 写入落地后抛异常：租约已丢 → 补偿再入队 + re-raise
    store.release.set()
    with pytest.raises(RuntimeError, match="add exploded"):
        await asyncio.wait_for(task, timeout=10)
    assert store.exploded

    leftover = [m.id for m in store.added_in_window]
    assert leftover and set(leftover) <= {m.id for m in await store.list(user["user_id"])}, (
        "前置失败：慢 worker 抛错前的写入应已落地"
    )
    jobs = await _memsync_jobs(note_id)
    compensating = [j for j in jobs if j.status == "pending"]
    assert len(compensating) == 1, (
        f"add 抛异常也必须留下补偿 pending：{[j.status for j in jobs]}"
    )
    note = await _note(note_id)
    assert note.memory_status == "synced", "丢租约的异常执行不得改写 memory_status"

    # 排空：补偿任务 delete_by_note 清掉两批写入，收敛为恰好一组
    await run_jobs()
    note = await _note(note_id)
    assert note.memory_status == "synced"
    items = await _items(store, user["user_id"], note_id)
    assert items and all(m.source_rev == note.rev for m in items)
    assert len({m.id for m in items}) == len(items), "终态恰好一组记忆"
    assert not set(leftover) & {m.id for m in items}, "慢 worker 的残留最终被收敛清掉"
