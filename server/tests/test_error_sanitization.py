"""INV-14（threads8 P1，runner.py:71）：异常原文与用户内容不落日志/库/
错误响应——只记类型名 + 粗粒度错误码（app/errors.py::safe_error_summary）。

覆盖：摘要函数的纯单元行为（消息含伪造密钥/笔记正文时也只有 type/code）、
job 异常的日志与 jobs.last_error、worker 常驻循环日志（无 traceback）、
422 不回显 input、500 体固定文案。"""
import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from app.errors import error_code, safe_error_summary, safe_error_text
from app.models import Job
from tests.conftest import auth_headers


def test_safe_error_summary_type_and_code_only():
    """消息里带伪造密钥与笔记正文：摘要只有类型名+错误码，无任何消息文本。"""
    marker = "pa_sk_NOTESECRET_000 我的笔记正文与待办"
    exc = RuntimeError(f"db statement failed: {marker}")
    assert safe_error_summary(exc) == {"type": "RuntimeError", "code": "internal"}
    text = safe_error_text(exc)
    assert text == "RuntimeError/internal"
    assert marker not in text
    assert "RuntimeError" in text

    # SQLAlchemy 层次：IntegrityError/OperationalError 都是 StatementError
    # 的子类，粗码必须落到最具体的那个
    stmt_err = StatementError("boom", "SELECT …", {"p1": marker}, None)
    assert error_code(stmt_err) == "db_statement"
    integ = IntegrityError("m", "s", {"p": marker}, Exception("orig"))
    assert error_code(integ) == "db_integrity"
    assert safe_error_text(stmt_err) == "StatementError/db_statement"
    assert marker not in safe_error_text(stmt_err)

    import json

    assert error_code(json.JSONDecodeError("Expecting value", "doc", 0)) == "json_parse"
    assert error_code(TimeoutError()) == "timeout"
    assert error_code(ConnectionError("refused")) == "network"

    from app.errors import ApiError

    assert error_code(ApiError(409, "conflict", "rev 过期")) == "conflict"


async def test_job_exception_not_logged_nor_persisted_raw(client, user, caplog):
    """threads8 P1（runner.py:71）：任务异常消息含笔记正文与伪造密钥 →
    caplog 与 jobs.last_error 均不含原文、只含类型名/错误码；job 照常按
    重试/失败语义收尾（不因脱敏改变控制流）。"""
    from app.pipeline import runner

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "明天上午十点交周报"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]

    marker = "pa_sk_JOBSECRET_111 SQL参数里的笔记正文"

    async def exploding_execute_job(kind, payload, *, claim=None):
        raise RuntimeError(f"statement failed: {marker}")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "execute_job", exploding_execute_job)
    try:
        with caplog.at_level(logging.WARNING, logger="app.pipeline.runner"):
            await runner.run_pending_jobs(retry_delay_seconds=0)
    finally:
        monkeypatch.undo()

    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text, "job 失败日志不得带异常原文（INV-14）"

    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        jobs = (
            await session.execute(select(Job).where(Job.note_id == note_id))
        ).scalars().all()
    assert jobs, "前置失败：应有已失败的任务"
    assert all(j.status == "failed" for j in jobs), [j.status for j in jobs]
    for j in jobs:
        assert "RuntimeError" in (j.last_error or "")
        assert marker not in (j.last_error or ""), "last_error 不得存异常原文"


async def test_worker_loop_exception_logged_sanitized_no_traceback(monkeypatch, caplog):
    """常驻 worker 循环的异常：只记类型名+错误码，无原文、无 exc_info/
    traceback，且循环不退出（下一轮照常轮询）。"""
    import asyncio

    from app.jobs import worker as worker_mod

    marker = "pa_sk_LOOPSECRET_222 网络错误原文"
    calls = {"n": 0}

    class _LoopSettings:
        database_url = "sqlite+aiosqlite:///:memory:"
        memory_backend = "disabled"
        worker_poll_interval_seconds = 0
        worker_batch_size = 5

    async def _no_create_all():
        ...

    async def failing_run_pending_jobs(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"worker boom: {marker}")
        # 跳出常驻循环：CancelledError 是 BaseException，不被 except Exception 捕获
        raise asyncio.CancelledError()

    monkeypatch.setattr(worker_mod, "get_settings", lambda: _LoopSettings())
    monkeypatch.setattr(worker_mod, "startup_create_all", _no_create_all)
    monkeypatch.setattr(worker_mod, "run_pending_jobs", failing_run_pending_jobs)

    with caplog.at_level(logging.ERROR, logger="worker"), pytest.raises(asyncio.CancelledError):
        await worker_mod.main()

    assert calls["n"] == 2, "第一轮异常后循环必须继续（常驻进程不退出）"
    assert "worker loop error" in caplog.text
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text
    assert all(r.exc_info is None for r in caplog.records), "不得带 traceback"


async def test_validation_error_response_does_not_echo_input(client, user):
    """422 只回 type/loc/msg：input/ctx（请求原值整段回显）不落响应体。"""
    marker = "VALIDATION-ECHO-MARKER-笔记正文"
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": marker},  # 缺 text → input 回显整个 body
        headers=auth_headers(user),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert marker not in r.text, "422 不得回显请求原值（INV-14）"
    assert "Field required" in r.text, "msg 保留：客户端修正请求所需"


async def test_500_response_body_contains_no_exception_text(client, user, monkeypatch):
    """未处理异常（消息含用户内容）→ 500 体是固定文案，绝不回显异常文本。"""
    from app.api import notes as notes_api
    from app.main import app

    marker = "INTERNAL-MARKER-笔记正文 pa_sk_333"

    async def exploding_allocate_seq(session, *args, **kwargs):
        raise RuntimeError(f"seq allocation exploded: {marker}")

    monkeypatch.setattr(notes_api, "allocate_seq", exploding_allocate_seq)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        r = await raw.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "会被炸掉的正文"},
            headers=auth_headers(user),
        )
    assert r.status_code == 500, r.text
    body = r.json()["error"]
    assert body["code"] == "internal_error"
    assert body["message"] == "internal server error"
    assert marker not in r.text, "500 体不得含异常文本（INV-14）"
