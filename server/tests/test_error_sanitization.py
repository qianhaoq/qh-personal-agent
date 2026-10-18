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


async def test_500_no_sentinel_in_any_logger_record(client, user, monkeypatch, caplog):
    """threads9 P1（main.py:56）/ INV-14：未处理异常由最外层用户中间件
    SafeErrorFallbackMiddleware 兜住并**永不 re-raise**——Starlette 的
    ServerErrorMiddleware 调完 Exception handler 后总是 re-raise、原始
    traceback 进 uvicorn.error 的那条路不可达。异常消息含哨兵（笔记正文
    + 伪造密钥）时，**所有** logger 的捕获记录（消息、格式化参数、
    exc_text）均不含哨兵；兜底日志只含类型名+错误码。"""
    from app.api import notes as notes_api
    from app.main import app

    marker = "pa_sk_ALLLOGSECRET_444 SQL参数里的笔记正文"

    async def exploding_allocate_seq(session, *args, **kwargs):
        raise RuntimeError(f"seq allocation exploded: {marker}")

    monkeypatch.setattr(notes_api, "allocate_seq", exploding_allocate_seq)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=transport, base_url="http://test") as raw:
            r = await raw.post(
                "/v1/notes",
                json={"client_note_id": str(uuid.uuid4()), "text": "会被炸掉的正文"},
                headers=auth_headers(user),
            )
    assert r.status_code == 500, r.text
    assert marker not in r.text

    assert caplog.records, "兜底中间件应记一行错误日志"
    for record in caplog.records:
        blob = (
            record.getMessage()
            + "\n"
            + str(record.args)
            + "\n"
            + (record.exc_text or "")
        )
        assert marker not in blob, f"logger {record.name} 落了异常原文（INV-14）"
        assert record.exc_info is None or record.exc_text is None, (
            f"logger {record.name} 不得带 traceback"
        )
    fallback = [
        rec for rec in caplog.records if "unhandled exception" in rec.getMessage()
    ]
    assert fallback, "应有兜底中间件的错误日志"
    assert any("RuntimeError" in rec.getMessage() for rec in fallback), (
        "兜底日志只含类型名（safe_error_text）"
    )


async def test_500_real_uvicorn_no_raw_traceback_in_logs(user, monkeypatch):
    """threads9 P1（main.py:56）/ INV-14：真实 uvicorn 进程（默认日志
    配置）下复现评审场景——未处理异常不再被 ServerErrorMiddleware
    re-raise 进 uvicorn.error：兜底中间件吞掉后 uvicorn 收到的是普通
    500 响应，全进程无哨兵、无 traceback。给 uvicorn/uvicorn.error/
    uvicorn.access/sqlalchemy.engine/root 各挂捕获 handler（uvicorn.access
    propagate=False，caplog 的 root handler 收不到，必须逐 logger 挂）。"""
    import asyncio

    import uvicorn

    from app.api import notes as notes_api
    from app.main import app

    marker = "pa_sk_UVICORNSECRET_555 SQL绑定参数里的笔记正文"

    async def exploding_allocate_seq(session, *args, **kwargs):
        raise RuntimeError(f"seq allocation exploded: {marker}")

    monkeypatch.setattr(notes_api, "allocate_seq", exploding_allocate_seq)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.DEBUG)
    watched = ["", "uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"]
    saved_levels: dict[str, int] = {}
    for name in watched:
        lg = logging.getLogger(name)
        saved_levels[name] = lg.level
        lg.addHandler(capture)
        lg.setLevel(logging.DEBUG)

    # lifespan=off：不触发 app lifespan 的 create_all/dispose（引擎由测试
    # 夹具管理），本测试只关心异常传播与日志面
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="debug", lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn 未能在 10s 内启动"
        port = server.servers[0].sockets[0].getsockname()[1]

        async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            r = await http.post(
                "/v1/notes",
                json={"client_note_id": str(uuid.uuid4()), "text": "会被炸掉的正文"},
                headers=auth_headers(user),
            )
        assert r.status_code == 500, r.text
        body = r.json()["error"]
        assert body["code"] == "internal_error"
        assert body["message"] == "internal server error"
        assert marker not in r.text
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        for name in watched:
            lg = logging.getLogger(name)
            lg.removeHandler(capture)
            lg.setLevel(saved_levels[name])

    assert records, "应捕获到 uvicorn/access 的日志"
    for record in records:
        blob = (
            record.getMessage()
            + "\n"
            + str(record.args)
            + "\n"
            + (record.exc_text or "")
        )
        assert marker not in blob, f"logger {record.name} 落了异常原文（INV-14）"
        assert record.exc_text is None, f"logger {record.name} 带 traceback：{record.getMessage()}"


def test_third_party_http_loggers_pinned_to_warning():
    """threads9 / INV-14：create_app 把 httpx/httpcore/openai 的 logger 钉
    到 >= WARNING——环境/库默认级别漂移、未来版本把请求细节（LLM 请求体
    含笔记正文）打进 INFO 日志时，应用层不设防就会把用户内容漏进日志。"""
    from app.logs import _PINNED_LOGGERS
    from app.main import create_app

    for name in _PINNED_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    create_app()
    for name in _PINNED_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


async def test_access_log_records_template_only_no_query_or_path_values(
    client, user, caplog
):
    """threads9 / INV-14：uvicorn access log 已在 compose/Dockerfile 关闭
    （请求行含完整 query），进程内唯一的请求级日志 AccessLogMiddleware
    只记 method + 路由模板 + status + 耗时——/v1/search 的 query 值、
    路径参数原值、未匹配路径都不进访问日志。"""
    q_marker = "ACCESSQ-笔记正文-marker"
    path_marker = "SECRETPATHNOTEID"
    nomatch_marker = "SECRETNOMATCH"
    with caplog.at_level(logging.INFO, logger="access"):
        r1 = await client.get(
            "/v1/search", params={"q": q_marker}, headers=auth_headers(user)
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.get(f"/v1/notes/{path_marker}", headers=auth_headers(user))
        assert r2.status_code == 404, r2.text
        r3 = await client.get(f"/no/such/{nomatch_marker}")
        assert r3.status_code == 404, r3.text

    text = caplog.text
    assert q_marker not in text, "访问日志不得含 query 值（INV-14）"
    assert path_marker not in text, "访问日志不得含路径参数原值（INV-14）"
    assert nomatch_marker not in text, "未匹配路由只记 (unmatched)，不打原路径"
    assert "GET /v1/search -> 200" in text
    assert "GET /v1/notes/{note_id} -> 404" in text
    assert "GET (unmatched) -> 404" in text
