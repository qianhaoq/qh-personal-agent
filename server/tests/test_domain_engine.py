"""Super-agent 第一刀：领域引擎（Trigger → Ticket → Worker facade）。

覆盖 docs/architecture-superagent.md 的领域路径：
- engine.receive 开单（observe 默认）与 dedupe_key 幂等；
- PersonalPolicy 的准入（off 忽略；observe 只授被动动作面）；
- POST /v1/notes 钩子：Ticket 建立、契约不变、不再 enqueue extract、
  旁路故障不影响 202；
- organize worker facade 驱动现有 pipeline（不删 pipeline）。
全部内存/MockLLM，不依赖外部密钥。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from app.domain.engine import Engine, get_engine, reset_engine
from app.models import Job
from app.policy.personal_policy import ACCEPT_MODE_OFF, PersonalPolicy
from app.sources.voice_http import HttpApiSource, VoiceIngress
from app.workers import OrganizeNoteWorker
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
async def _clean_engine():
    """进程内引擎与其它测试文件的 API 调用共享，逐测清空再复原。"""
    reset_engine()
    yield
    reset_engine()



def _silence_schedule(monkeypatch):
    """禁止 API 自动调度，便于单测显式调用 OrganizeNoteWorker。"""
    import app.workers.dispatch as dispatch

    monkeypatch.setattr(dispatch, "schedule_organize", lambda work_item: None)

def _note_body(text: str = "明天下午三点提醒我给张总回电话") -> dict:
    return {"client_note_id": str(uuid.uuid4()), "text": text, "source": "app"}


def _receive_note(engine: Engine, *, note_id: str, rev: int = 1, user_id: str = "usr_t"):
    return HttpApiSource().note_uploaded(engine, user_id=user_id, note_id=note_id, rev=rev)


async def test_receive_note_uploaded_opens_pending_organize_work_item():
    engine = Engine()
    receipt = _receive_note(engine, note_id="nte_1")

    assert not receipt.duplicate
    assert receipt.decision.outcome == "accept"
    item = receipt.work_item
    assert item is not None
    assert item.kind == "organize_note"
    assert item.status == "pending"
    assert item.note_id == "nte_1"
    assert item.note_rev == 1
    assert item.thread_key == "note:nte_1"
    assert item.mandate.policy_version == "v0"

    types = [e.type for e in engine.events()]
    assert "trigger_recorded" in types
    assert "work_item_opened" in types


async def test_receive_is_idempotent_on_dedupe_key():
    engine = Engine()
    r1 = _receive_note(engine, note_id="nte_1", rev=1)
    r2 = _receive_note(engine, note_id="nte_1", rev=1)

    # 同 note 同 rev 重放：duplicate 回执、不新开 Ticket
    assert r2.duplicate
    assert r2.work_item is not None and r2.work_item.id == r1.work_item.id
    assert len(engine.work_items_for_note("nte_1")) == 1

    # 新 rev（PATCH 重整理）是新事件：另开一张 Ticket，同 Thread
    r3 = _receive_note(engine, note_id="nte_1", rev=2)
    assert not r3.duplicate
    assert len(engine.work_items_for_note("nte_1")) == 2
    assert r3.work_item.thread_key == r1.work_item.thread_key


async def test_policy_v0_observe_default_grants_only_passive_scope():
    policy = PersonalPolicy()
    assert policy.accept_mode == "observe"

    engine = Engine(policy=policy)
    receipt = _receive_note(engine, note_id="nte_1")
    assert receipt.decision.outcome == "accept"

    scope = receipt.work_item.mandate.scope
    assert "write_note_derived" in scope
    assert "suggest_reminder" in scope
    # observe：不授权静默建闹钟/外发——提醒只能以「建议」形态出现
    assert "schedule_reminder" not in scope
    assert "send_outbound" not in scope


async def test_policy_off_ignores_trigger_without_ticket():
    engine = Engine(policy=PersonalPolicy(accept_mode=ACCEPT_MODE_OFF))
    receipt = _receive_note(engine, note_id="nte_1")

    assert receipt.decision.outcome == "ignore"
    assert receipt.work_item is None
    assert engine.work_items_for_note("nte_1") == []


async def test_voice_ingress_placeholder_records_source_kind():
    engine = Engine()
    receipt = VoiceIngress().note_uploaded(
        engine, user_id="usr_t", note_id="nte_v", rev=1
    )

    assert receipt.work_item is not None
    trigger = engine.get_trigger(receipt.trigger_id)
    assert trigger is not None
    assert trigger.source == "voice_ingress"
    assert trigger.kind == "note_uploaded"


async def test_notes_api_hook_opens_ticket_without_double_extract(client, user, monkeypatch):
    _silence_schedule(monkeypatch)
    body = _note_body()
    resp = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]

    engine = get_engine()
    items = engine.work_items_for_note(note_id)
    assert len(items) == 1
    assert items[0].kind == "organize_note"
    assert items[0].status == "pending"
    assert items[0].user_id == user["user_id"]

    # 幂等重放（同 client_note_id）：不触发第二张 Ticket
    resp2 = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert resp2.status_code == 202
    assert resp2.json()["note_id"] == note_id
    assert len(engine.work_items_for_note(note_id)) == 1

    # S2 B 刀：notes 路径不再 enqueue extract（Ticket 驱动）
    async with get_sessionmaker()() as session:
        extract_jobs = (
            await session.execute(select(Job).where(Job.kind == "extract"))
        ).scalars().all()
    assert len(extract_jobs) == 0


async def test_engine_hook_failure_never_breaks_notes_api(client, user, monkeypatch):
    def boom(self, engine, *, user_id, note_id, rev):
        raise RuntimeError("engine down")

    monkeypatch.setattr(HttpApiSource, "note_uploaded", boom)
    resp = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    assert resp.status_code == 202, resp.text


async def test_organize_worker_facade_runs_pipeline_and_completes_ticket(client, user, monkeypatch):
    _silence_schedule(monkeypatch)
    resp = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id = resp.json()["note_id"]

    engine = get_engine()
    item = engine.work_items_for_note(note_id)[0]
    outcome = await OrganizeNoteWorker(engine).run(item)

    assert outcome["status"] == "done"
    assert engine.get_work_item(item.id).status == "done"
    types = [e.type for e in engine.events()]
    assert "work_item_running" in types
    assert "work_item_completed" in types

    # facade 确实驱动了现有 pipeline：笔记被 MockLLM 整理
    got = await client.get(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert got.status_code == 200
    assert got.json()["status"] == "organized"
    assert got.json()["title"]


async def test_organize_worker_facade_records_failure(client, user, monkeypatch):
    _silence_schedule(monkeypatch)
    resp = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id = resp.json()["note_id"]

    async def boom(note_id, enqueued_rev=None, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.pipeline.extract.extract_note", boom)

    engine = get_engine()
    item = engine.work_items_for_note(note_id)[0]
    outcome = await OrganizeNoteWorker(engine).run(item)

    assert outcome["status"] == "failed"
    assert outcome["error"]  # 安全摘要（类型/错误码），非异常原文
    assert engine.get_work_item(item.id).status == "failed"

# ------------------------------------------------- S2 第一刀 A：认领/报告 CAS


async def test_claim_only_pending_work_item_succeeds():
    engine = Engine()
    item = _receive_note(engine, note_id="nte_1").work_item

    # pending → 认领成功：running + 持有者写入
    assert engine.mark_running(item.id, worker_id="wrk_a") is True
    assert item.status == "running"
    assert item.worker_id == "wrk_a"

    # 已 running：另一 worker 的二次认领被拒，原 holder 不变
    assert engine.mark_running(item.id, worker_id="wrk_b") is False
    assert item.status == "running"
    assert item.worker_id == "wrk_a"

    # 未知 work_item_id 仍 raise
    with pytest.raises(ValueError):
        engine.mark_running("wki_unknown", worker_id="wrk_a")


async def test_concurrent_organize_worker_second_claim_rejected(client, user, monkeypatch):
    _silence_schedule(monkeypatch)
    resp = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id = resp.json()["note_id"]

    extract_calls: list[str] = []

    async def stub_extract(note_id, enqueued_rev=None, **kwargs):
        extract_calls.append(note_id)
        await asyncio.sleep(0)  # 让出事件循环，放大两路 run 的竞争窗口

    monkeypatch.setattr("app.pipeline.extract.extract_note", stub_extract)

    engine = get_engine()
    item = engine.work_items_for_note(note_id)[0]
    outcomes = await asyncio.gather(
        OrganizeNoteWorker(engine).run(item),
        OrganizeNoteWorker(engine).run(item),
    )

    # 仅认领成功的一路进 extract；另一路被拒且不破坏终态
    assert len(extract_calls) == 1
    statuses = sorted(o["status"] for o in outcomes)
    assert statuses == ["claim_failed", "done"]
    assert engine.get_work_item(item.id).status == "done"


async def test_late_fail_does_not_overwrite_done():
    engine = Engine()
    item = _receive_note(engine, note_id="nte_1").work_item

    assert engine.mark_running(item.id, worker_id="wrk_a") is True
    assert engine.complete(item.id, result={"note_id": "nte_1"}, worker_id="wrk_a") is True
    assert item.status == "done"

    # 迟到的 fail（worker B）：done 不可被覆盖、error 不写入
    assert engine.fail(item.id, "late crash", worker_id="wrk_b") is False
    assert item.status == "done"
    assert item.error is None


async def test_late_complete_ignored_when_not_holder():
    engine = Engine()
    item = _receive_note(engine, note_id="nte_1").work_item

    assert engine.mark_running(item.id, worker_id="wrk_a") is True

    # 非持有者 complete：no-op，仍 running、result 不写
    assert engine.complete(item.id, result={"bogus": True}, worker_id="wrk_b") is False
    assert item.status == "running"
    assert item.result is None

    # 持有者随后 complete 成功
    assert engine.complete(item.id, result={"note_id": "nte_1"}, worker_id="wrk_a") is True
    assert item.status == "done"
    assert item.result == {"note_id": "nte_1"}


async def test_notes_create_no_extract_job_ticket_drives_organize(client, user, run_jobs):
    """B 刀：create 不入队 extract；Ticket + Worker 整理到 organized。"""
    from app.db import get_sessionmaker
    from app.models import Note

    body = _note_body("明天下午三点提醒我给张总回电话")
    resp = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]

    async with get_sessionmaker()() as session:
        extract_jobs = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalars().all()
    assert extract_jobs == []

    engine = get_engine()
    items = engine.work_items_for_note(note_id)
    assert len(items) == 1
    assert items[0].kind == "organize_note"

    await run_jobs()
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    item = get_engine().get_work_item(items[0].id)
    assert note.status == "organized"
    assert item is not None and item.status == "done"


async def test_notes_patch_opens_new_ticket_and_reorganizes(client, user, run_jobs):
    """B 刀：PATCH 正文开新 Ticket、无 extract job、仍有 memory_sync，并重整理。"""
    from app.db import get_sessionmaker
    from app.models import Note

    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes",
        json=_note_body("明天上午十点交周报"),
        headers=headers,
    )
    note_id = created.json()["note_id"]
    await run_jobs()
    engine = get_engine()
    assert len(engine.work_items_for_note(note_id)) == 1

    got = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()
    patched = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "改成纯闲聊，没有任何待办", "rev": got["rev"]},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    items = engine.work_items_for_note(note_id)
    assert len(items) == 2

    async with get_sessionmaker()() as session:
        extract_jobs = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalars().all()
        mem_jobs = (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note_id)
            )
        ).scalars().all()
    assert extract_jobs == []
    assert mem_jobs, "PATCH 仍应 enqueue memory_sync"

    await run_jobs()
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.text == "改成纯闲聊，没有任何待办"


async def test_memory_sync_still_enqueued_after_ticket_extract(client, user, run_jobs, monkeypatch):
    """B 刀：Ticket 路径跑完后仍有 memory_sync（可用 stub 计数 extract 调用）。"""
    from app.db import get_sessionmaker
    from app.models import Note
    from app.pipeline import extract as extract_mod

    calls: list[str] = []
    real = extract_mod.extract_note

    async def stub(note_id, enqueued_rev=None, **kwargs):
        calls.append(note_id)
        return await real(note_id, enqueued_rev=enqueued_rev, **kwargs)

    monkeypatch.setattr(extract_mod, "extract_note", stub)

    resp = await client.post(
        "/v1/notes", json=_note_body("记得明天买牛奶"), headers=auth_headers(user)
    )
    note_id = resp.json()["note_id"]
    await run_jobs()

    assert calls == [note_id]
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        mem_jobs = (
            await session.execute(
                select(Job).where(Job.kind == "memory_sync", Job.note_id == note_id)
            )
        ).scalars().all()
    assert note.status == "organized"
    assert note.memory_status == "synced"
    assert mem_jobs and any(j.status == "done" for j in mem_jobs)
