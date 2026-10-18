"""extract 抽取流水线（MockLLM）。记忆写删的测试见 test_memory_sync.py。"""
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import Job, Note, Reminder
from app.timeutil import as_utc, to_iso
from tests.conftest import auth_headers


def _freeze_now(monkeypatch, fixed: datetime) -> None:
    """固定 extract 眼中的“服务器当前时间”。

    整理会丢弃早于服务器当前时间的 fire_at（见 test_fire_at_before_
    server_now_discarded），用固定过去/未来时间的测试不固定 now 的话，
    断言会随真实时钟漂移。
    """
    monkeypatch.setattr("app.pipeline.extract.utcnow", lambda: fixed)


async def _create_and_extract(client, user, text, run_jobs) -> Note:
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


async def _extract_job(note_id: str) -> Job:
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()


async def test_extract_produces_title_tags_reminders(client, user, run_jobs, monkeypatch):
    # 该用例的 recorded_at（2026-10-16）与推算出的 fire_at 相对真实时钟
    # 可能已是过去，固定 now 保证“未来时间仍生成”的断言不随日期漂移
    _freeze_now(monkeypatch, datetime(2026, 10, 16, 8, 0, tzinfo=UTC))
    note = await _create_and_extract(
        client, user, "明天下午三点提醒我去医院体检，还要买火车票", run_jobs
    )
    assert note.status == "organized"
    assert note.title
    assert note.tags  # 健康/旅行 之类
    assert any("体检" in t["text"] for t in note.todos)
    assert note.rev == 2  # organized 后 rev+1
    assert note.llm_calls == 1  # 只调用一次 LLM
    assert note.memory_status == "synced"  # 同事务入队的 memory_sync 已跑完

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


async def test_llm_called_once_across_retries_and_idempotent(client, user, run_jobs):
    note = await _create_and_extract(client, user, "记得明天买牛奶", run_jobs)
    # 再跑一遍队列（无新 job）不会重复抽取
    await run_jobs()
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        refreshed = await session.get(Note, note.id)
    assert refreshed.llm_calls == 1
    assert refreshed.status == "organized"


async def test_repatch_reextracts(client, user, run_jobs):
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": "cn_r1", "text": "随便记一句"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    await run_jobs()

    # 整理后 rev 变成 2，客户端要先拉最新版本再改
    current = await client.get(
        f"/v1/notes/{note_id}", headers={"authorization": f"Bearer {user['access_token']}"}
    )
    rev = current.json()["rev"]
    assert rev == 2

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
    assert note.llm_calls == 2  # 重新抽取了一次


async def test_first_llm_failure_leaves_no_side_effects(client, user, monkeypatch):
    """LLM 第一次失败：note/提醒/记忆都不留半成品。

    LLM 调用发生在任何写事务之前，失败时根本没有副作用可回滚。job 回
    pending 重试；记忆此时也不该有任何写入（memory_sync 只在 apply 提交
    后才由同事务入队）。
    """
    from app.db import get_sessionmaker
    from app.memory.base import get_memory_store
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

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    processed = await run_pending_jobs(limit=1, retry_delay_seconds=0)
    assert processed == 1

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.text == text  # 原文未被规范化覆盖
        assert note.status == "uploaded"  # 无中间态残留
        assert note.rev == 1
        assert note.llm_calls == 0
        assert (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all() == []
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalars().one()
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.note_rev == 1
        # INV-14（threads8 P1）：last_error 只记类型名+错误码，不落异常原文
        assert "RuntimeError" in (job.last_error or "")
        assert "transient llm failure" not in (job.last_error or "")
        # 抽取未成功：没有 memory_sync 被入队，也没有任何记忆
        memsync = (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note_id)
            )
        ).scalars().all()
    assert memsync == []
    assert [m for m in await get_memory_store().list(user["user_id"])] == []

    # LLM 恢复后重试成功
    monkeypatch.undo()
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"


async def test_extract_skips_when_rev_moved_marks_done(client, user, monkeypatch):
    """INV-2.2：入队时的 rev 与执行时不一致：任务跳过 = done，无副作用。

    PATCH 已重新入队新 rev 的 extract，旧任务执行时一次 LLM 都不调用。
    """
    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "第一版内容"},
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]  # rev=1，job 记录 note_rev=1

    # 抽取跑之前先 PATCH（rev 1→2，重新入队 rev=2 的 job）
    patch = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "明天上午十点交周报", "rev": 1},
        headers=auth_headers(user),
    )
    assert patch.status_code == 200

    llm_calls = {"n": 0}

    class CountingLLM:
        async def chat(self, messages, response_format=None):
            llm_calls["n"] += 1
            return json.dumps(
                {"title": "t", "tags": [], "todos": [], "suggested_reminders": []},
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return CountingLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    # 只跑一个：最旧的是 rev=1 的任务，它必须跳过 = done（零副作用）
    processed = await run_pending_jobs(limit=1, retry_delay_seconds=0)
    assert processed == 1
    async with get_sessionmaker()() as session:
        jobs = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalars().all()
        note = await session.get(Note, note_id)
    old = next(j for j in jobs if j.note_rev == 1)
    assert old.status == "done"
    assert llm_calls["n"] == 0
    assert note.status == "uploaded"  # 旧任务没有写任何整理产物

    # 新 rev 的任务随后正常抽取
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.text == "明天上午十点交周报"
    assert note.llm_calls == 1


async def test_extract_attempts_exhausted_marks_note_failed_by_rev(client, user, monkeypatch):
    """INV-2.5：extract 连续失败耗尽 → note.status=failed（按入队 rev 条件）。"""
    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "会失败的笔记"},
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]

    class BoomLLM:
        async def chat(self, messages, response_format=None):
            raise RuntimeError("llm down")

    async def fake_get_llm():
        return BoomLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    await run_pending_jobs(retry_delay_seconds=0)

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()
    assert job.status == "failed"
    assert note.status == "failed"
    assert note.memory_status == "pending"  # extract 失败不触碰 memory_status


async def test_extract_finalize_skips_note_patched_after_enqueue(client, user, monkeypatch):
    """INV-2/6：最后一次失败收尾前用户 PATCH 了笔记 → 不把新 rev 的笔记标 failed。

    失败收尾按入队 rev 条件更新，rowcount=0 时不动 note；新 rev 的抽取
    任务正常完成。
    """
    from app.db import get_sessionmaker
    from app.pipeline import runner
    from app.pipeline.runner import run_pending_jobs

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

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    finalize_calls = {"n": 0}
    real_finalize = runner._finalize

    async def finalize_with_mid_race_patch(maker, claimed, error, retry_delay_seconds, worker_id):
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
        await real_finalize(maker, claimed, error, retry_delay_seconds, worker_id)

    monkeypatch.setattr(runner, "_finalize", finalize_with_mid_race_patch)

    # 只跑 3 个：旧任务 3 次尝试全部失败（判 failed），新 rev 的任务留在队列里
    processed = await run_pending_jobs(limit=3, retry_delay_seconds=0)
    assert processed == 3

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status != "failed"  # 新 rev 未被过期任务的失败收尾覆盖
    assert note.status == "uploaded"  # PATCH 后等待重新抽取

    # 新 rev 的任务随后正常完成，记忆也收敛
    await run_pending_jobs(retry_delay_seconds=0)
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        jobs = (
            await session.execute(select(Job).where(Job.note_id == note_id))
        ).scalars().all()
    assert {j.status for j in jobs} == {"failed", "done"}
    assert note.status == "organized"  # 不是 failed：收尾条件更新未命中新 rev
    assert note.rev == 3  # PATCH +1，新任务抽取再 +1
    assert note.text == "用户改过的最新内容"
    assert note.llm_calls == 1  # 只有新任务真正调用了 LLM
    assert note.memory_status == "synced"


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

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
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


async def test_llm_boundary_fire_at_overflow_discarded(client, user, monkeypatch, run_jobs):
    """threads6 P2 / INV-7：LLM 输出的时间套用与请求体相同的 ±14h 安全界
    （utc_safe_datetime）——`astimezone` 溢出或越界的 fire_at 只丢弃该条
    suggested reminder，笔记照常 organized；正常未来时间保留。旧行为里
    naive `9999-12-31T23:59:59`（配 America/New_York）会在换 UTC 时
    OverflowError 上抛，耗尽重试把整条笔记标 failed。"""
    from zoneinfo import ZoneInfo

    from app.db import get_sessionmaker
    from app.schemas import utc_safe_datetime

    # 界判定函数本身：溢出 / 越界 / 正常
    assert utc_safe_datetime(datetime(9999, 12, 31, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))) is None
    assert utc_safe_datetime(datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)) is None
    assert utc_safe_datetime(datetime(1, 1, 1, tzinfo=UTC)) is None
    assert utc_safe_datetime(datetime(2027, 1, 1, 9, 0, tzinfo=UTC)) == datetime(
        2027, 1, 1, 9, 0, tzinfo=UTC
    )

    _freeze_now(monkeypatch, datetime(2026, 10, 16, 20, 0, tzinfo=UTC))

    class BoundaryLLM:
        async def chat(self, messages, response_format=None):
            return json.dumps(
                {
                    "title": "t",
                    "tags": [],
                    "todos": [],
                    "suggested_reminders": [
                        # aware 越上界（> 9998-12-31 预留界）
                        {"title": "上界外", "fire_at": "9999-12-31T23:59:59+00:00"},
                        # naive + 用户时区 America/New_York：换 UTC 必溢出
                        {"title": "换算溢出", "fire_at": "9999-12-31T23:59:59"},
                        # aware 越下界（< 0001-01-02 预留界）
                        {"title": "下界外", "fire_at": "0001-01-01T00:00:00+00:00"},
                        # 正常未来时间：必须保留
                        {"title": "正常提醒", "fire_at": "2027-01-01T09:00:00+00:00"},
                    ],
                },
                ensure_ascii=False,
            )

    async def fake_get_llm():
        return BoundaryLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "随口一提",
            "recorded_at": "2026-10-16T08:00:00+00:00",
            "timezone": "America/New_York",
        },
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]
    await run_jobs()

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        # 界外时间只能丢提醒，不能丢笔记
        assert note.status == "organized", "换算溢出必须丢弃提醒而不是把笔记标 failed"
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
        assert [r_.title for r_ in reminders] == ["正常提醒"]
        assert to_iso(reminders[0].fire_at) == "2027-01-01T09:00:00+00:00"


async def test_fire_at_before_server_now_discarded(client, user, monkeypatch, run_jobs):
    """按 recorded_at 推算的提醒可能落在服务器当前时间之前（旧录音补抽取时
    “明天”是录音那天的明天）。早于 now 的 fire_at 不生成建议提醒；
    正常的未来时间仍生成。
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
            assert note.status == "organized"  # 提醒被丢弃不影响抽取本身
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

    monkeypatch.setattr(get_settings(), "job_max_attempts", 5)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "重试次数配置"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]
    job = await _extract_job(note_id)
    assert job.max_attempts == 5


async def test_llm_output_unsafe_control_chars_stripped(client, user, run_jobs, monkeypatch):
    """threads8 P2 / INV-7（extract.py:120）：LLM 输出的字符串含 NUL、C0
    控制符、DEL、孤立 UTF-16 代理对时（json.loads 接受它们，写 PG
    text/JSON 列时编码失败是确定性的）→ 落库前剥掉；剥完为空的
    tags/todos/提醒标题整条丢弃；笔记照常 organized、不 500、不重试。"""

    class DirtyLLM:
        async def chat(self, messages, response_format=None):
            return (
                '{"title": "标\\u0000题带NUL", '
                '"tags": ["工\\u0000作", "\\u0000", "孤立\\ud800代理"], '
                '"todos": [{"text": "买\\u0000牛奶", "due": "2030-01-01\\u0000"}, '
                '{"text": "\\u0000"}, {"text": "带\\ud800代理的待办"}], '
                '"suggested_reminders": [{"title": "提\\u0000醒交周报", '
                '"fire_at": "2030-01-01T09:00:00+08:00"}, '
                '{"title": "\\u0000", "fire_at": "2030-02-01T09:00:00+08:00"}]}'
            )

    async def fake_get_llm():
        return DirtyLLM()

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "一条会收到脏字符串的笔记",
        },
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]
    await run_jobs()

    from datetime import UTC, datetime

    from app.db import get_sessionmaker
    from app.models import Note, Reminder

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        reminders = (
            await session.execute(select(Reminder).where(Reminder.note_id == note_id))
        ).scalars().all()
    assert note.status == "organized", "脏字符只影响个别字段，不该把笔记拖进重试/失败"
    assert note.title == "标题带NUL"
    assert note.tags == ["工作", "孤立代理"], "剥完为空的 tag 整条丢弃"
    assert note.todos == [
        {"text": "买牛奶", "due": "2030-01-01"},
        {"text": "带代理的待办"},
    ], "todo 文本/due 同样清干净，纯脏字符的 todo 丢弃"
    assert [r.title for r in reminders] == ["提醒交周报"], "纯脏字符的提醒标题丢弃"
    assert as_utc(reminders[0].fire_at) == datetime(2030, 1, 1, 1, 0, tzinfo=UTC)
    for value in [note.title, note.tags, note.todos] + [r.title for r in reminders]:
        joined = value if isinstance(value, str) else str(value)
        assert "\x00" not in joined and "\x7f" not in joined
