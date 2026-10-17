"""自动整理流水线（MockLLM）。"""
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.memory.base import get_memory_store
from app.models import Job, Note, Reminder
from app.timeutil import to_iso
from tests.conftest import auth_headers


def _freeze_now(monkeypatch, fixed: datetime) -> None:
    """固定 organize 眼中的“服务器当前时间”。

    整理会丢弃早于服务器当前时间的 fire_at（见 test_fire_at_before_
    server_now_discarded），用固定过去/未来时间的测试不固定 now 的话，
    断言会随真实时钟漂移。
    """
    monkeypatch.setattr("app.pipeline.organize.utcnow", lambda: fixed)


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


async def test_organize_produces_title_tags_reminders(client, user, run_jobs, monkeypatch):
    # 该用例的 recorded_at（2026-10-16）与推算出的 fire_at 相对真实时钟
    # 可能已是过去，固定 now 保证“未来时间仍生成”的断言不随日期漂移
    _freeze_now(monkeypatch, datetime(2026, 10, 16, 8, 0, tzinfo=UTC))
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
    """LLM 第一次失败：note/提醒/记忆都不留半成品。

    LLM 调用发生在任何写事务之前，失败时根本没有副作用可回滚。之前
    （单事务时代）认领、执行、失败标记在同一个事务里，异常被吞掉后
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


async def test_memory_failure_retries_backfill_only(client, user, monkeypatch):
    """P2：记忆写入移到写事务提交之后，失败靠 job 重试幂等补写。

    第一次：LLM 成功、note 已 organized（rev+1、提醒已建），store.add
    在写入之后失败 → DB 副作用保留，job 回 pending 重试。
    重试：organize 发现 note 已 organized 且 memory_synced=False，跳过
    LLM 只补写记忆（delete_by_note 后 add），即使上次 add 已部分写入
    也不会重复。
    """
    from app.db import get_sessionmaker
    from app.memory.inmemory import InMemoryStore
    from app.pipeline.runner import run_pending_jobs

    class WriteThenFailStore(InMemoryStore):
        """第一次 add：先写入再抛错（模拟“写入成功但结果确认前失败”）。"""

        def __init__(self):
            super().__init__()
            self.add_calls = 0

        async def add(self, user_id, text, note_id=None):
            self.add_calls += 1
            item = await super().add(user_id, text, note_id=note_id)
            if self.add_calls == 1:
                raise RuntimeError("memory backend down after write")
            return item

    store = WriteThenFailStore()
    monkeypatch.setattr("app.pipeline.organize.get_memory_store", lambda: store)

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "周末和朋友聚餐"},
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]

    # 第一次执行：整理落库，记忆失败，job 待重试
    processed = await run_pending_jobs(limit=1, retry_delay_seconds=0)
    assert processed == 1
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalars().one()
    assert note.status == "organized"  # DB 副作用已提交，不再随记忆失败回滚
    assert note.memory_synced is False
    assert note.rev == 2 and note.llm_calls == 1
    assert job.status == "pending"
    assert job.attempts == 1
    assert "memory backend down" in (job.last_error or "")
    assert store.add_calls == 1
    assert len([m for m in await store.list(user["user_id"]) if m.note_id == note_id]) == 1

    # 重试：只补写记忆，不再调 LLM / 重新整理
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalars().one()
    assert job.status == "done"
    assert job.attempts == 2
    assert note.status == "organized"
    assert note.memory_synced is True
    assert note.rev == 2  # 没有再整理
    assert note.llm_calls == 1  # 没有再调 LLM
    assert store.add_calls == 2
    items = [m for m in await store.list(user["user_id"]) if m.note_id == note_id]
    assert len(items) == 1  # delete_by_note 后再 add：最终写入且不重复


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
    client, user, run_jobs, monkeypatch
):
    """相对时间以 recorded_at 为参照；规范化文本只喂 LLM，不覆盖原文。"""
    from app.db import get_sessionmaker

    # fire_at 是 2026-01-02：把 now 固定在它之前，只验证“以 recorded_at
    # 为参照”本身，不和“早于服务器 now 丢弃”的规则互相干扰
    _freeze_now(monkeypatch, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
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


async def test_fire_at_before_server_now_discarded(client, user, monkeypatch, run_jobs):
    """P2：按 recorded_at 推算的提醒可能落在服务器当前时间之前（旧录音
    补整理时“明天”是录音那天 的明天）。早于 now 的 fire_at 不生成
    建议提醒；正常的未来时间仍生成。
    """
    from app.db import get_sessionmaker

    _freeze_now(monkeypatch, datetime(2026, 10, 16, 20, 0, tzinfo=UTC))
    headers = auth_headers(user)

    # 2020 年的旧录音：“明天九点”按 recorded_at 推算落在 now 之前 → 不生成
    old = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "明天九点开会",
            "recorded_at": "2020-01-01T00:00:00+00:00",
        },
        headers=headers,
    )
    # 正常录音：recorded_at 在 now 之后 → 生成
    new = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "明天九点开会",
            "recorded_at": "2026-10-20T08:00:00+00:00",
        },
        headers=headers,
    )
    old_id, new_id = old.json()["note_id"], new.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        for note_id in (old_id, new_id):
            note = await session.get(Note, note_id)
            assert note.status == "organized"  # 提醒被丢弃不影响整理本身
        old_reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == old_id))
        ).scalars().all()
        new_reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == new_id))
        ).scalars().all()
    assert old_reminders == []
    assert len(new_reminders) == 1
    # “明天九点”：2026-10-20（当地 16:00）的明天 09:00 +08
    assert to_iso(new_reminders[0].fire_at) == "2026-10-21T01:00:00+00:00"


async def test_enqueue_uses_configured_max_attempts(client, user, monkeypatch):
    """入队的 job 继承 settings.job_max_attempts，而不是写死默认值。"""
    from app.config import get_settings
    from app.db import get_sessionmaker

    monkeypatch.setattr(get_settings(), "job_max_attempts", 5)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "重试次数配置"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    async with get_sessionmaker()() as session:
        job = (
            await session.execute(
                select(Job).where(Job.kind == "organize_note", Job.payload_json.contains(note_id))
            )
        ).scalars().one()
    assert job.max_attempts == 5


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


async def test_failed_finalize_skips_note_patched_after_enqueue(client, user, monkeypatch):
    """P1-C：最后一次失败收尾前用户 PATCH 了笔记 → 不把新 rev 的笔记标 failed。

    旧收尾对 note 无条件置 failed：拿着旧 rev 的任务失败收尾时会把用户
    刚 PATCH 出来的新版本也标成 failed。现在按入队 rev 条件更新，
    rowcount=0 时不动 note；新 rev 的整理任务正常完成。
    """
    import uuid

    from app.db import get_sessionmaker
    from app.pipeline import runner
    from app.pipeline.runner import run_pending_jobs
    from tests.conftest import auth_headers

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "会失败且会被中途改掉的笔记"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.organize.get_llm_client", fake_get_llm)

    finalize_calls = {"n": 0}
    real_finalize = runner._finalize_failure

    async def finalize_with_mid_race_patch(maker, claimed, error, retry_delay_seconds):
        finalize_calls["n"] += 1
        if finalize_calls["n"] == 3:
            # 第 3 次（最后一次）失败收尾前，用户 PATCH：rev+1 并入队新任务，
            # LLM 恢复（模拟“旧任务失败与用户修改并发，新任务随后成功”）
            monkeypatch.undo()
            rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
            patched = await client.patch(
                f"/v1/notes/{note_id}",
                json={"text": "用户改过的最新内容", "rev": rev},
                headers=headers,
            )
            assert patched.status_code == 200, patched.text
        await real_finalize(maker, claimed, error, retry_delay_seconds)

    monkeypatch.setattr(runner, "_finalize_failure", finalize_with_mid_race_patch)

    # 只跑 3 个：旧任务 3 次尝试全部失败（判 failed），新 rev 的任务留在队列里。
    # 此时检查收尾有没有污染用户的新版本（下一次运行才会执行新任务）
    processed = await run_pending_jobs(limit=3, retry_delay_seconds=0)
    assert processed == 3

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status != "failed"  # 新 rev 未被过期任务的失败收尾覆盖
    assert note.status == "uploaded"  # PATCH 后等待重新整理

    # 新 rev 的任务随后正常完成
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        jobs = (
            await session.execute(
                select(Job).where(Job.payload_json.contains(note_id))
            )
        ).scalars().all()
    assert {j.status for j in jobs} == {"failed", "done"}
    assert note.status == "organized"  # 不是 failed：收尾条件更新未命中新 rev
    assert note.rev == 3  # PATCH +1，新任务整理再 +1
    assert note.text == "用户改过的最新内容"
    assert note.llm_calls == 1  # 只有新任务真正调用了 LLM
