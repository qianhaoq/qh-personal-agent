"""自动整理流水线（MockLLM）。"""
import json
import uuid

from sqlalchemy import select

from app.memory.base import get_memory_store
from app.models import Job, Note, Reminder
from app.timeutil import to_iso


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


async def test_first_llm_failure_leaves_no_side_effects(client, user, monkeypatch):
    """LLM 第一次失败：整理事务整体回滚，note/提醒/记忆都不留半成品。

    之前认领、执行、失败标记在同一个事务里，异常被吞掉后
    “organizing 状态 + 规范化文本”等部分副作用会随事务一起提交。
    """
    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    text = "嗯，那个，明天上午十点交周报"
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

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("transient llm failure")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)
    processed = await run_pending_jobs(limit=1, retry_delay_seconds=0)
    assert processed == 1

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.text == text  # 原文未被规范化覆盖
        assert note.status == "uploaded"  # organizing 已回滚
        assert note.rev == 1
        assert note.llm_calls == 0
        assert (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all() == []
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalars().one()
        assert job.status == "pending"
        assert job.attempts == 1
        assert "transient llm failure" in (job.last_error or "")
    assert [m for m in await get_memory_store().list(user["user_id"])] == []

    # LLM 恢复后重试成功
    monkeypatch.undo()
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"


async def test_stale_organize_job_skipped_after_patch(client, user, run_jobs):
    """入队时的 rev 与执行时不一致：任务跳过，由 PATCH 重新入队的任务整理。"""
    from app.db import get_sessionmaker

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "第一版内容"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]  # rev=1，job 记录 rev=1

    # 整理跑之前先 PATCH（rev 1→2，重新入队 rev=2 的 job）
    patch = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "明天上午十点交周报", "rev": 1},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    assert patch.status_code == 200
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.llm_calls == 1  # 旧 job 被跳过，只有新 job 调了一次 LLM
    assert note.status == "organized"
    assert note.text == "明天上午十点交周报"


async def test_relative_time_uses_recorded_at_and_keeps_original_text(
    client, user, run_jobs
):
    """相对时间以 recorded_at 为参照；规范化文本只喂 LLM，不覆盖原文。"""
    from app.db import get_sessionmaker

    text = "嗯，那个，明天上午十点交周报"
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": text,
            "recorded_at": "2026-01-01T00:00:00+00:00",
        },
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.text == text  # 用户原文不动
        assert note.status == "organized"
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
    # “明天上午十点”以 2026-01-01（当地 08:00）为参照 → 2026-01-02 10:00 +08
    assert len(reminders) == 1
    assert to_iso(reminders[0].fire_at) == "2026-01-02T02:00:00+00:00"


async def test_past_or_broken_fire_at_discarded(client, user, monkeypatch, run_jobs):
    """早于参照时间或解析失败的 fire_at：不生成建议提醒。"""
    from app.db import get_sessionmaker

    class PastLLM:
        async def chat(self, messages, response_format=None):
            return json.dumps(
                {
                    "title": "t",
                    "tags": [],
                    "todos": [],
                    "suggested_reminders": [
                        {"title": "过期提醒", "fire_at": "2000-01-01T00:00:00+00:00"},
                        {"title": "坏时间", "fire_at": "not-a-date"},
                    ],
                },
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return PastLLM()

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "随口一提",
            "recorded_at": "2026-10-16T08:00:00+00:00",
        },
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.status == "organized"
        assert (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all() == []


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
