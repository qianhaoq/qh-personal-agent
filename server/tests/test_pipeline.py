"""自动整理流水线（MockLLM）。"""

from app.memory.base import get_memory_store
from app.models import Note, Reminder


async def _create_and_organize(client, user, text, run_jobs) -> Note:
    import uuid

    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": text,
            "recorded_at": "2026-10-16T08:00:00+00:00",
        },
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    await run_jobs()
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await session.get(Note, note_id)


async def test_organize_produces_title_tags_reminders(client, user, run_jobs):
    note = await _create_and_organize(
        client, user, "明天下午三点提醒我去医院体检，还要买火车票", run_jobs
    )
    assert note.status == "organized"
    assert note.title
    assert note.tags  # 健康/旅行 之类
    assert any("体检" in t["text"] for t in note.todos)
    assert note.rev == 2  # organized 后 rev+1
    assert note.llm_calls == 1  # 只调用一次 LLM

    # 建议提醒已写入
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note.id))
        ).scalars().all()
    assert len(reminders) >= 1
    assert all(r.status == "suggested" for r in reminders)
    fire = [r for r in reminders if r.fire_at is not None]
    assert fire, "应至少解析出一个 fire_at"


async def test_organize_writes_memory(client, user, run_jobs):
    note = await _create_and_organize(client, user, "周末和朋友聚餐", run_jobs)
    store = get_memory_store()
    items = [m for m in await store.list(user["user_id"]) if m.note_id == note.id]
    assert items, "笔记应写入 MemoryStore"


async def test_llm_called_once_across_retries_and_idempotent(client, user, run_jobs):
    note = await _create_and_organize(client, user, "记得明天买牛奶", run_jobs)
    # 再跑一遍队列（无新 job）不会重复整理
    await run_jobs()
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        refreshed = await session.get(Note, note.id)
    assert refreshed.llm_calls == 1
    assert refreshed.status == "organized"


async def test_repatch_reorganizes(client, user, run_jobs):
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": "cn_r1", "text": "随便记一句"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id, rev = r.json()["note_id"], r.json()["rev"]
    await run_jobs()

    # 整理后 rev 变成 2，客户端要先拉最新版本再改
    current = await client.get(
        f"/v1/notes/{note_id}", headers={"authorization": f"Bearer {user['access_token']}"}
    )
    rev = current.json()["rev"]

    patch = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "明天上午九点提醒我开会", "rev": rev},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    assert patch.status_code == 200
    await run_jobs()

    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.llm_calls == 2  # 重新整理了一次


async def test_failure_marks_note_failed(client, user, monkeypatch):
    """LLM 连续失败 3 次后 note.status=failed。"""
    import uuid

    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "会失败的笔记"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)
    await run_pending_jobs(retry_delay_seconds=0)

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "failed"
