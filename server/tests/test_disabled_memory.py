"""disabled 记忆后端（threads7 拆分，INV-10.5/INV-5）。

生产记忆后端（mem0 + pgvector）已拆分至 feat/mem0-backend 分支；`disabled`
是拆分后到该 PR 落地前的显式空后端。这里锁定它的三件事：

1. 后端契约（INV-3）的空实现：所有接口返回空/0、绝不做任何外部写；
2. memory_sync 在空后端下平凡收敛：照常运行、memory_status 标 synced
   （不把 disabled 当失败重试）；
3. 读路径不炸：/v1/search 返回空 items、/v1/ask 走「无相关记忆」分支，
   都不 500。
"""
import uuid

from app.memory.base import get_memory_store, set_memory_store
from app.memory.disabled import DisabledMemoryStore
from app.models import Note
from tests.conftest import auth_headers


async def test_disabled_store_empty_contract():
    """MemoryStore 契约的空实现：add/search/list 空、三个 delete 都是 0。"""
    store = DisabledMemoryStore()
    assert await store.add("usr_1", "我对花生过敏", note_id="n1", source_rev=1) == []
    assert await store.search("usr_1", "花生") == []
    assert await store.list("usr_1") == []
    assert await store.delete("usr_1", ["m1"]) == 0
    assert await store.delete_by_note("usr_1", "n1") == 0
    assert await store.delete_all("usr_1") == 0


def test_factory_returns_disabled_store(monkeypatch):
    """MEMORY_BACKEND=disabled 时工厂返回 DisabledMemoryStore（不是静默
    退化成 inmemory）。"""
    from app.config import get_settings
    from app.memory import base as memory_base

    monkeypatch.setattr(memory_base, "_store", None)
    monkeypatch.setattr(get_settings(), "memory_backend", "disabled")
    store = get_memory_store()
    assert isinstance(store, DisabledMemoryStore)
    # 顺手复位，避免本用例把 _store 留给后续测试（monkeypatch 只还原
    # 被 setattr 的属性，_store 是被工厂自己重建的）
    set_memory_store(DisabledMemoryStore())


async def test_disabled_memory_sync_marks_synced(client, user, run_jobs):
    """INV-5 在空后端下平凡成立：memory_sync 照常跑完并标 synced——
    「记忆 == f(note)」在后端永远没有记忆时恒真，不标 failed、不重试。"""
    set_memory_store(DisabledMemoryStore())
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "我对花生过敏"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]
    await run_jobs()

    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note is not None and note.memory_status == "synced"
    # 后端确实没有任何记忆（不是"写了但查不到"的假装在跑）
    assert await get_memory_store().list(user["user_id"]) == []


async def test_disabled_search_and_ask_return_empty_not_500(client, user, run_jobs):
    """读路径：/v1/search 空 items、/v1/ask 走「无相关记忆」分支——
    功能关闭表现为确定的空结果，不是 500。"""
    set_memory_store(DisabledMemoryStore())
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "我对花生过敏"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    await run_jobs()

    headers = auth_headers(user)
    r = await client.get("/v1/search", params={"q": "过敏"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}

    r = await client.post("/v1/ask", json={"text": "我对什么过敏？"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "我没有相关记录。", "citations": []}
