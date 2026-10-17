"""删除后记忆不复活：QA 复测回归（PR #2）。

复现路径：monkeypatch 让 store.add 延迟（asyncio.sleep 证明窗口），
worker 跑到 note 已 organized、记忆尚未写入（memory_synced=False、
add 仍在等待）的中间态，此刻并发调用真实 DELETE（204）。之后等
organize 与 cleanup_memory 任务全部结束，GET /v1/search 与 POST /v1/ask
都不得返回该笔记的原文/引用。

两层防线分别单独验证（monkeypatch 关掉另一层）：
1. “add 后复查”（organize._sync_memory）：关掉 cleanup_memory 兜底，
   仅靠 add 之后对 note 的复查清掉刚写入的记忆；
2. “删除后入队 cleanup”：关掉复查（模拟复查读到删除前快照/被绕过），
   仅靠幂等的 cleanup_memory job 兜底清掉。

两个库都跑：交错由 store 钩子控制（DELETE 是 add 等待期间发出的真实
请求），不依赖数据库层面的并发语义。
"""
import asyncio
import uuid

from sqlalchemy import select, update

from app.memory.base import set_memory_store
from app.memory.inmemory import InMemoryStore
from app.models import Note
from tests.conftest import auth_headers

_NOTE_TEXT = "这条笔记的记忆会在写入途中被并发删除"


class DelayedAddStore(InMemoryStore):
    """目标笔记的第一次 add 先 sleep 一个窗口再写入：DELETE 必须落进窗口。"""

    def __init__(self):
        super().__init__()
        self.target_note_id: str | None = None
        self.delete_task: asyncio.Task | None = None
        self.entered = asyncio.Event()
        self.delete_landed_in_window: bool | None = None

    async def add(self, user_id, text, note_id=None):
        if note_id == self.target_note_id and not self.entered.is_set():
            self.entered.set()
            # 证明窗口：这 0.3s 内 DELETE 必须完成（真实并发请求）
            await asyncio.sleep(0.3)
            self.delete_landed_in_window = bool(self.delete_task and self.delete_task.done())
        return await super().add(user_id, text, note_id=note_id)


async def _assert_no_reference(client, user, note_id: str, text: str) -> None:
    """GET /v1/search 与 POST /v1/ask 都不能返回原文/该 note 的引用。"""
    headers = auth_headers(user)
    search = await client.get("/v1/search", params={"q": text[:20]}, headers=headers)
    assert search.status_code == 200, search.text
    assert [m for m in search.json()["items"] if m["note_id"] == note_id] == []

    ask = await client.post("/v1/ask", json={"text": text[:20]}, headers=headers)
    assert ask.status_code == 200, ask.text
    assert [c for c in ask.json()["citations"] if c["note_id"] == note_id] == []
    assert text not in ask.json()["answer"]


async def _run_to_delayed_add(client, user, run_jobs, store: DelayedAddStore) -> str:
    """创建笔记并把 worker 推进到“已 organized、add 正在等待”的中间态。

    随后在 add 的延迟窗口内并发发出 DELETE（真实请求），等 organize
    结束。返回 note_id。
    """
    from app.pipeline.runner import run_pending_jobs

    # SQLite 测试库跨测试复用：先排空残留 job，保证进入延迟 add 的是本测试的任务
    await run_jobs()

    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": _NOTE_TEXT},
        headers=headers,
    )
    assert created.status_code == 202, created.text
    note_id = created.json()["note_id"]
    store.target_note_id = note_id

    worker = asyncio.create_task(run_pending_jobs(limit=1))
    await asyncio.wait_for(store.entered.wait(), timeout=5)

    # 中间态成立：整理已提交（organized），记忆尚未写入（add 正在 sleep）
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.memory_synced is False

    # 并发 DELETE：add 仍在窗口内等待（完整走 handler：锁行、入队 cleanup、
    # 提交后清理），而不是直接改库
    delete_task = asyncio.ensure_future(client.delete(f"/v1/notes/{note_id}", headers=headers))
    store.delete_task = delete_task
    deleted = await asyncio.wait_for(delete_task, timeout=3)
    assert deleted.status_code == 204, deleted.text

    await asyncio.wait_for(worker, timeout=10)
    assert store.delete_landed_in_window is True, "DELETE 必须落在 add 的延迟窗口内"
    return note_id


async def test_recheck_layer_alone_cleans_memory(client, user, run_jobs, monkeypatch):
    """第一层单独验证：关掉 cleanup_memory 兜底，仅靠 add 后复查清掉记忆。"""
    from app.db import get_sessionmaker
    from app.models import Job
    from app.pipeline import organize as organize_mod

    async def cleanup_disabled(note_id, user_id):
        return None

    monkeypatch.setattr(organize_mod, "cleanup_note_memory", cleanup_disabled)

    store = DelayedAddStore()
    set_memory_store(store)

    note_id = await _run_to_delayed_add(client, user, run_jobs, store)

    # cleanup 被关掉成 no-op：job 也会执行并完成，但清记忆只能靠复查层
    await run_jobs()
    async with get_sessionmaker()() as session:
        cleanups = (
            await session.execute(
                select(Job).where(
                    Job.kind == "cleanup_memory", Job.payload_json.contains(note_id)
                )
            )
        ).scalars().all()
    assert {j.status for j in cleanups} == {"done"}
    assert [m for m in await store.list(user["user_id"]) if m.note_id == note_id] == []
    await _assert_no_reference(client, user, note_id, _NOTE_TEXT)


async def test_cleanup_layer_alone_cleans_memory(client, user, run_jobs, monkeypatch):
    """第二层单独验证：关掉 add 后复查，复活确实发生后由 cleanup job 兜底清掉。"""
    from app.pipeline import organize as organize_mod

    async def sync_memory_no_recheck(maker, snapshot, expected_rev):
        """无复查版 _sync_memory：add 完直接置位（模拟复查读到删除前快照）。"""
        store_ = organize_mod.get_memory_store()
        await store_.delete_by_note(snapshot.user_id, snapshot.note_id)
        await store_.add(snapshot.user_id, snapshot.text, note_id=snapshot.note_id)
        async with maker() as session, session.begin():
            await session.execute(
                update(Note).where(Note.id == snapshot.note_id).values(memory_synced=True)
            )

    monkeypatch.setattr(organize_mod, "_sync_memory", sync_memory_no_recheck)

    store = DelayedAddStore()
    set_memory_store(store)

    note_id = await _run_to_delayed_add(client, user, run_jobs, store)

    # 复查被关掉：organize 结束时记忆确实“复活”了（证明竞态窗口真实存在），
    # 接下来只能指望 cleanup_memory job 兜底
    assert [m for m in await store.list(user["user_id"]) if m.note_id == note_id], (
        "前置失败：复查被关掉后记忆应短暂复活"
    )

    await run_jobs()
    assert [m for m in await store.list(user["user_id"]) if m.note_id == note_id] == []
    await _assert_no_reference(client, user, note_id, _NOTE_TEXT)
