"""Persistent runtime contract and concurrency regressions (SQLite and PG).

These replace S1 in-memory/facade assertions with Store state, real Worker
execution, atomic reports and crash recovery. Existing queue regressions remain.
"""
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.domain.engine import Engine
from app.domain.store import Store
from app.models import Job, Note, SaThread, SaTrigger, SaWorkItem
from app.policy.personal_policy import PersonalPolicy
from app.runtime import run_pending_work
from app.sources.base import build_note_uploaded_trigger
from app.sources.voice_http import HttpApiSource, VoiceIngress
from app.timeutil import utcnow
from app.workers import OrganizeNoteWorker
from tests.conftest import auth_headers, requires_postgres


def trigger(note_id="nte_1", rev=1):
    return build_note_uploaded_trigger("http_api", user_id="usr_t", note_id=note_id, rev=rev)


async def upload(client, user):
    resp = await client.post("/v1/notes", headers=auth_headers(user), json={
        "client_note_id": str(uuid.uuid4()), "text": "明天提醒我给张总回电话"})
    assert resp.status_code == 202, resp.text
    return resp.json()["note_id"]


async def test_receive_note_uploaded_opens_pending_organize_work_item():
    receipt = await Engine().receive(trigger())
    item = receipt.work_item
    assert not receipt.duplicate and receipt.decision.outcome == "accept"
    assert (item.kind, item.status, item.note_id, item.note_rev) == (
        "organize_note", "pending", "nte_1", 1)
    assert item.thread_key == "note:nte_1"
    assert item.mandate.policy_version == "v1"
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(SaThread)) == 1
        assert await session.scalar(select(func.count()).select_from(SaTrigger)) == 1


async def test_receive_is_idempotent_on_dedupe_key():
    first = await Engine().receive(trigger())
    second = await Engine(Store()).receive(trigger())
    third = await Engine().receive(trigger(rev=2))
    assert second.duplicate and second.work_item.id == first.work_item.id
    assert not third.duplicate
    items = await Engine().work_items_for_note("nte_1")
    assert len(items) == 2 and len({x.thread_key for x in items}) == 1


async def test_policy_observe_default_grants_only_passive_scope():
    item = (await Engine().receive(trigger())).work_item
    assert item.mandate.scope == ("write_note_derived", "suggest_reminder")
    for mode in ("observe", "auto"):
        engine = Engine(policy=PersonalPolicy(accept_mode=mode))
        ask = await HttpApiSource().ask_requested(engine, user_id="usr_t", payload={"text": "问题"})
        assert ask.work_item.mandate.scope == ("search_memory", "answer_ask")
    with pytest.raises(ValueError):
        PersonalPolicy(accept_mode="typo")


async def test_policy_off_ignores_trigger_without_ticket():
    receipt = await Engine(policy=PersonalPolicy(accept_mode="off")).receive(trigger())
    assert receipt.decision.outcome == "ignore" and receipt.work_item is None
    assert await Engine().work_items_for_note("nte_1") == []


async def test_voice_ingress_placeholder_records_source_kind():
    receipt = await VoiceIngress().note_uploaded(Engine(), user_id="usr_t", note_id="n", rev=1)
    assert (await Engine().get_trigger(receipt.trigger_id)).source == "voice_ingress"


async def test_notes_api_hook_opens_ticket_without_double_extract(client, user):
    body = {"client_note_id": str(uuid.uuid4()), "text": "原文"}
    first = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    second = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    items = await Engine().work_items_for_note(first.json()["note_id"])
    assert len(items) == 1 and items[0].status == "pending"
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "extract")) == 0
    await OrganizeNoteWorker(Engine()).run(items[0])
    async with get_sessionmaker()() as session:
        note = await session.get(Note, first.json()["note_id"])
        assert note.status == "organized" and note.title and note.rev == 2
    assert (await Engine().get_work_item(items[0].id)).status == "done"


async def test_engine_hook_failure_never_breaks_notes_api(client, user, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("private intake message")
    with monkeypatch.context() as patch:
        patch.setattr(HttpApiSource, "note_uploaded", boom)
        note_id = await upload(client, user)
    assert "private intake message" not in caplog.text
    assert await Engine().work_items_for_note(note_id) == []
    # Committed upload with a missing Trigger is recovered from DB by polling.
    assert await run_pending_work() == 1
    assert (await Engine().work_items_for_note(note_id))[0].status == "done"


async def test_cas_owner_and_terminal_state():
    engine = Engine()
    item = (await engine.receive(trigger())).work_item
    assert not await engine.complete(item.id, {}, worker_id="a")
    assert not await engine.fail(item.id, RuntimeError(), worker_id="a")
    assert await engine.claim(item.id, "a")
    assert not await Engine().claim(item.id, "b")
    assert not await engine.complete(item.id, {}, worker_id="b")
    assert not await engine.fail(item.id, RuntimeError(), worker_id="b")
    assert await engine.complete(item.id, {"ok": True}, worker_id="a")
    assert not await engine.fail(item.id, RuntimeError("late"), worker_id="a")
    assert not await engine.complete(item.id, {"ok": False}, worker_id="a")
    assert not await engine.claim(item.id, "c")
    assert (await Engine().get_work_item(item.id)).result == {"ok": True}


async def test_claim_honors_retry_backoff_without_consuming_attempts():
    engine = Engine()
    item = (await engine.receive(trigger())).work_item
    assert await engine.claim(item.id, "first-execution")
    assert await engine.fail(item.id, RuntimeError(), worker_id="first-execution",
                             retry=True, retry_delay_seconds=3600)
    assert await engine.pending() == []
    claims = await asyncio.gather(*[
        Engine().claim(item.id, f"early-{i}") for i in range(8)
    ])
    assert not any(claims)
    saved = await engine.get_work_item(item.id)
    assert saved.status == "pending" and saved.attempts == 1
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(SaWorkItem).where(SaWorkItem.id == item.id).values(
            run_after=utcnow() - timedelta(seconds=1)))
    assert [ready.id for ready in await engine.pending()] == [item.id]
    claims = await asyncio.gather(*[
        Engine().claim(item.id, f"ready-{i}") for i in range(8)
    ])
    assert sum(claims) == 1
    saved = await engine.get_work_item(item.id)
    assert saved.status == "running" and saved.attempts == 2


async def test_pending_and_done_survive_pool_and_engine_restart():
    engine = Engine()
    pending = (await engine.receive(trigger("n1"))).work_item
    done = (await engine.receive(trigger("n2"))).work_item
    assert await engine.claim(done.id, "worker")
    assert await engine.complete(done.id, {"answer": "结果"}, worker_id="worker")
    await dispose_engine()
    restarted = Engine(Store(get_sessionmaker()))
    assert (await restarted.get_work_item(pending.id)).status == "pending"
    restored = await restarted.get_work_item(done.id)
    assert restored.status == "done" and restored.result == {"answer": "结果"}
    receipt = await restarted.receive(trigger("n1"))
    assert receipt.duplicate and receipt.work_item.id == pending.id


async def test_patch_opens_new_ticket_and_stale_result_is_discarded(client, user, monkeypatch):
    note_id = await upload(client, user)
    item = (await Engine().work_items_for_note(note_id))[0]
    from app.workers import extract_impl
    original = extract_impl.prepare_output
    async def prepare_then_patch(*args, **kwargs):
        output = await original(*args, **kwargs)
        response = await client.patch(f"/v1/notes/{note_id}", headers=auth_headers(user),
                                      json={"rev": 1, "text": "新版本"})
        assert response.status_code == 200
        return output
    with monkeypatch.context() as patch:
        patch.setattr(extract_impl, "prepare_output", prepare_then_patch)
        await OrganizeNoteWorker(Engine()).run(item)
    items = await Engine().work_items_for_note(note_id)
    assert len(items) == 2 and items[0].status == "done" and items[1].status == "pending"
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.status == "uploaded" and note.title is None and note.rev == 2
        assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "extract")) == 0
    await run_pending_work()
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.status == "organized" and note.text == "新版本" and note.rev == 3


async def test_ask_ticket_answer_same_request_and_channel(client, user):
    resp = await client.post("/v1/ask", headers=auth_headers(user), json={"text": "我记过什么？"})
    assert resp.status_code == 200 and resp.json() == {"answer": "我没有相关记录。", "citations": []}
    async with get_sessionmaker()() as session:
        item = (await session.scalars(select(SaWorkItem).where(SaWorkItem.kind == "answer_ask"))).one()
        assert item.status == "done" and item.result == resp.json()
        assert item.worker_id and item.attempts == 1


async def test_ask_failure_is_safe_and_ticket_failed(client, user, monkeypatch):
    async def boom(*args):
        raise RuntimeError("private question")
    monkeypatch.setattr("app.workers.answer.answer_question", boom)
    resp = await client.post("/v1/ask", headers=auth_headers(user), json={"text": "问题"})
    assert resp.status_code == 500 and "private question" not in resp.text
    async with get_sessionmaker()() as session:
        item = (await session.scalars(select(SaWorkItem))).one()
        assert item.status == "failed" and item.error == "RuntimeError/internal"


async def test_expired_assignment_recovered_and_old_report_rejected(client, user):
    note_id = await upload(client, user)
    engine = Engine()
    item = (await engine.work_items_for_note(note_id))[0]
    assert await engine.claim(item.id, "old-execution")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(SaWorkItem).where(SaWorkItem.id == item.id).values(
            locked_at=utcnow()-timedelta(seconds=get_settings().job_lock_timeout_seconds+1)))
    await engine.recover()
    assert (await Engine().get_work_item(item.id)).status == "pending"
    assert await engine.claim(item.id, "new-execution")
    assert not await engine.complete(item.id, {"late": True}, worker_id="old-execution")
    assert not await engine.fail(item.id, RuntimeError(), worker_id="old-execution")
    assert await engine.complete(item.id, {"ok": True}, worker_id="new-execution")


async def test_concurrent_intake_and_claim_single_assignment():
    receipts = await asyncio.gather(*[Engine(Store()).receive(trigger()) for _ in range(8)])
    assert len({r.work_item.id for r in receipts}) == 1
    assert sum(not r.duplicate for r in receipts) == 1
    item_id = receipts[0].work_item.id
    claims = await asyncio.gather(*[Engine(Store()).claim(item_id, f"execution-{i}") for i in range(8)])
    assert sum(claims) == 1
    owner = f"execution-{claims.index(True)}"
    reports = await asyncio.gather(
        Engine().complete(item_id, {"done": True}, worker_id=owner),
        Engine().fail(item_id, RuntimeError(), worker_id="intruder"))
    assert reports == [True, False]
    assert (await Engine().get_work_item(item_id)).status == "done"


async def test_late_organize_output_fenced_after_expiry(client, user, monkeypatch):
    note_id = await upload(client, user)
    engine = Engine()
    item = (await engine.work_items_for_note(note_id))[0]
    from app.workers import extract_impl
    prepare = extract_impl.prepare_output
    entered, release = asyncio.Event(), asyncio.Event()
    async def paused(*args, **kwargs):
        output = await prepare(*args, **kwargs)
        entered.set()
        await release.wait()
        return output
    monkeypatch.setattr(extract_impl, "prepare_output", paused)
    task = asyncio.create_task(OrganizeNoteWorker(engine).run(item))
    await asyncio.wait_for(entered.wait(), 5)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(SaWorkItem).where(SaWorkItem.id == item.id).values(
            attempts=get_settings().job_max_attempts,
            locked_at=utcnow()-timedelta(seconds=get_settings().job_lock_timeout_seconds+1)))
    await Engine().recover()
    release.set()
    assert (await asyncio.wait_for(task, 5))["status"] == "superseded"
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.status == "failed" and note.rev == 1 and note.title is None
        assert await session.scalar(select(func.count()).select_from(Job)) == 0
    assert (await Engine().get_work_item(item.id)).status == "failed"


async def test_channel_and_completion_rollback_together(client, user):
    note_id = await upload(client, user)
    item = (await Engine().work_items_for_note(note_id))[0]

    class FailingChannel:
        async def publish(self, session, current, output):
            await session.execute(update(Note).where(Note.id == note_id).values(title="partial"))
            raise RuntimeError("channel failed")

    engine = Engine(channel=FailingChannel())
    assert await engine.claim(item.id, "execution")
    with pytest.raises(RuntimeError, match="channel failed"):
        await engine.complete(item.id, {"ok": True}, worker_id="execution")
    # The report must be retryable and leave no partial business output.
    saved = await Engine().get_work_item(item.id)
    assert saved.status == "running" and saved.result is None
    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        assert note.title is None and note.status == "uploaded" and note.rev == 1


@requires_postgres
async def test_stale_candidate_claim_rechecks_retry_backoff_pg():
    """A's pending snapshot must not bypass B's committed retry backoff."""
    engine_a, engine_b = Engine(Store()), Engine(Store())
    item = (await engine_a.receive(trigger())).work_item
    candidate_read, retry_committed = asyncio.Event(), asyncio.Event()

    async def stale_claimant():
        candidates = await engine_a.pending()
        assert [candidate.id for candidate in candidates] == [item.id]
        candidate_read.set()
        await asyncio.wait_for(retry_committed.wait(), 5)
        return await engine_a.claim(candidates[0].id, "worker-a")

    async def retrying_worker():
        await asyncio.wait_for(candidate_read.wait(), 5)
        assert await engine_b.claim(item.id, "worker-b")
        assert await engine_b.fail(item.id, RuntimeError(), worker_id="worker-b",
                                  retry=True, retry_delay_seconds=3600)
        saved = await engine_b.get_work_item(item.id)
        assert saved.status == "pending" and saved.attempts == 1
        assert saved.run_after > utcnow()
        retry_committed.set()
        return saved

    claimed, retried = await asyncio.wait_for(
        asyncio.gather(stale_claimant(), retrying_worker()), 15)
    assert claimed is False
    saved = await engine_a.get_work_item(item.id)
    assert saved.status == "pending" and saved.worker_id is None
    assert saved.attempts == retried.attempts == 1
    assert saved.run_after == retried.run_after
