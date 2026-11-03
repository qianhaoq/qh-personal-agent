"""Watchdog repair regressions: intake gap rescan and lease recovery, no Tickets.

`repair` is the reusable repair half of `run_pending_work`: the standalone
`app.jobs.watchdog` Control Loop polls it alone (no OrganizeNoteWorker, no
memory queue), while the Worker poll still repairs before claiming Tickets.
"""
import asyncio
import logging
import types
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.config import get_settings
from app.db import get_sessionmaker
from app.domain.engine import Engine
from app.domain.store import Store
from app.models import SaTrigger, SaWorkItem
from app.runtime import repair
from app.sources.voice_http import HttpApiSource
from app.timeutil import utcnow
from tests.conftest import auth_headers, requires_postgres


async def _upload_with_failed_intake(client, user, monkeypatch):
    """Same shape as test_engine_hook_failure_never_breaks_notes_api: a
    committed upload whose post-commit intake hook failed leaves no Trigger."""
    async def boom(*args, **kwargs):
        raise RuntimeError("private intake message")
    with monkeypatch.context() as patch:
        patch.setattr(HttpApiSource, "note_uploaded", boom)
        resp = await client.post("/v1/notes", headers=auth_headers(user), json={
            "client_note_id": str(uuid.uuid4()), "text": "明天提醒我给张总回电话"})
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    assert await Engine().work_items_for_note(note_id) == []
    return note_id


async def test_repair_rescans_uploaded_note_missing_trigger(client, user, monkeypatch):
    note_id = await _upload_with_failed_intake(client, user, monkeypatch)
    # repair alone intakes the gap and opens the pending Ticket — Organize
    # has not run, so the note stays uploaded and no Worker is needed here.
    assert await repair() == 1
    items = await Engine().work_items_for_note(note_id)
    assert len(items) == 1 and items[0].status == "pending"
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(SaTrigger.dedupe_key).where(
            SaTrigger.dedupe_key == f"note_uploaded:{note_id}:1")) is not None
    # Second pass is a no-op: the stable dedupe key is present now.
    assert await repair() == 0
    assert len(await Engine().work_items_for_note(note_id)) == 1


async def test_repair_recovers_expired_assignment_and_old_report_rejected(client, user):
    resp = await client.post("/v1/notes", headers=auth_headers(user), json={
        "client_note_id": str(uuid.uuid4()), "text": "原文"})
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    engine = Engine()
    item = (await engine.work_items_for_note(note_id))[0]
    assert await engine.claim(item.id, "old-execution")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(SaWorkItem).where(SaWorkItem.id == item.id).values(
            locked_at=utcnow()-timedelta(seconds=get_settings().job_lock_timeout_seconds+1)))
    # Recover re-opens the lease but is not counted as an intake repair
    # (the Trigger is present, so the rescan finds no missing note).
    assert await repair() == 0
    assert (await Engine().get_work_item(item.id)).status == "pending"
    assert await engine.claim(item.id, "new-execution")
    assert not await engine.complete(item.id, {"late": True}, worker_id="old-execution")
    assert not await engine.fail(item.id, RuntimeError(), worker_id="old-execution")
    assert await engine.complete(item.id, {"ok": True}, worker_id="new-execution")


@requires_postgres
async def test_repair_race_with_claim_and_stale_reports_pg(client, user):
    """Watchdog 与 Worker 并存时的真实竞态：多个独立 repair/recover 进程扫
    同一个过期 Assignment，与 new-execution 的 claim/complete 以及
    old-execution 的迟到报告在 PG 行锁下重叠。

    恢复的 CAS（status=running AND locked_at < cutoff）必须：清不掉已换
    fresh locked_at 的新主、挡下过期执行方的 complete/fail；Ticket 最终以
    新主的结果 done 收尾，attempts 只被两次 claim 推进。SQLite 单写者模型
    下事务天然串行，构造不出该窗口（见 conftest 的 requires_postgres）。
    """
    resp = await client.post("/v1/notes", headers=auth_headers(user), json={
        "client_note_id": str(uuid.uuid4()), "text": "原文"})
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    item = (await Engine().work_items_for_note(note_id))[0]
    assert await Engine().claim(item.id, "old-execution")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(SaWorkItem).where(SaWorkItem.id == item.id).values(
            locked_at=utcnow()-timedelta(seconds=get_settings().job_lock_timeout_seconds+1)))

    reclaimed = asyncio.Event()

    async def reopen_watcher():
        # 老 token 不再持有 Assignment 的第一个观测点（被 recover 重开，
        # 或已被新主接管）——之后的迟到报告必须全部被拒。
        while True:
            saved = await Engine(Store()).get_work_item(item.id)
            if saved.status != "running" or saved.worker_id != "old-execution":
                reclaimed.set()
                return
            await asyncio.sleep(0.01)

    async def stale_reports():
        await asyncio.wait_for(reclaimed.wait(), 10)
        return (
            await Engine(Store()).complete(item.id, {"late": True}, worker_id="old-execution"),
            await Engine(Store()).fail(item.id, RuntimeError(), worker_id="old-execution"),
        )

    async def fresh_worker():
        # Boolean CAS claim：recover 重开（status=pending）前一直失败，
        # 拿到后立刻以新主身份 complete（Worker 侧的 claim→report）。
        while not await Engine(Store()).claim(item.id, "new-execution"):
            await asyncio.sleep(0.01)
        assert await Engine(Store()).complete(item.id, {"ok": True}, worker_id="new-execution")

    async def watchdog_passes(passes):
        # 独立进程形态：每次 repair/recover 都走各自的连接与事务，
        # 3 轮保证部分 CAS 与新主的 claim/complete 事务在行锁上重叠。
        for _ in range(passes):
            await repair()
            await Engine(Store()).recover()
            await asyncio.sleep(0)

    results = await asyncio.wait_for(asyncio.gather(
        reopen_watcher(), stale_reports(), fresh_worker(),
        watchdog_passes(3), watchdog_passes(3),
        Engine(Store()).recover(), Engine(Store()).recover(),
    ), 15)
    stale = results[1]
    assert stale == (False, False)
    saved = await Engine().get_work_item(item.id)
    assert saved.status == "done" and saved.result == {"ok": True}
    assert saved.worker_id == "new-execution" and saved.error is None
    # attempts 只被 old/new 两次 claim 推进；recover 与被拒的迟到报告都不动它。
    assert saved.attempts == 2
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(SaTrigger.dedupe_key).where(
            SaTrigger.dedupe_key == f"note_uploaded:{note_id}:1")) is not None


class _StopLoop(Exception):
    pass


async def test_watchdog_loop_only_repairs(monkeypatch, caplog):
    """One iteration of `python -m app.jobs.watchdog` calls repair with the
    Worker batch size, then sleeps the poll interval — never Tickets/queues."""
    from app.jobs import watchdog

    repaired, slept = [], []
    async def spy_repair(limit=100):
        repaired.append(limit)
        return 0
    async def stop_sleep(seconds):
        slept.append(seconds)
        raise _StopLoop
    monkeypatch.setattr(watchdog, "repair", spy_repair)
    monkeypatch.setattr(watchdog, "asyncio", types.SimpleNamespace(sleep=stop_sleep))
    with pytest.raises(_StopLoop), caplog.at_level(logging.ERROR):
        await watchdog.main()
    assert repaired == [get_settings().worker_batch_size]
    # 第一个 sleep 的异常被常驻循环吞掉（INV-14：记类型名+错误码后继续），
    # except 分支里的第二个 sleep 才把哨兵抛出循环。
    assert len(slept) == 2
    assert "watchdog loop error: _StopLoop/internal" in caplog.text
    assert "_StopLoop" in caplog.text and "Traceback" not in caplog.text
