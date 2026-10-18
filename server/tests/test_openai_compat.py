"""OpenAICompatClient 的 response_format 协商（json_schema / json_object / auto）。

DeepSeek 等供应商不支持 json_schema 变体（HTTP 400 "This response_format
type is unavailable now"）：auto 模式自动回落 json_object（schema 说明写进
system 消息）并把决定缓存在进程内。HTTP 用 httpx.MockTransport mock（替换
openai_compat 模块里的 httpx 引用，不动全局 httpx——同一测试里的 ASGI
客户端也在用）。JSON 校验保持在调用方（extract._parse_llm_json），两种
模式同一套。
"""
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.llm.openai_compat import (
    OpenAICompatClient,
    _reset_response_format_cache,
)
from app.models import Job, Note
from tests.conftest import auth_headers

# DeepSeek 实测的错误体形态（只用于驱动 mock，不含任何真实凭据）
_DEEPSEEK_STYLE_400 = {
    "error": {
        "message": "This response_format type is unavailable now",
        "type": "invalid_request_error",
        "code": "invalid_request_error",
    }
}

_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


@pytest.fixture(autouse=True)
def _clean_response_format_cache():
    _reset_response_format_cache()
    yield
    _reset_response_format_cache()


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    """把 openai_compat 模块里的 httpx 引用替换为注入 MockTransport 的替身，
    返回按序记录的请求列表（读 body 用 json.loads(request.content)）。"""
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    real_async_client = httpx.AsyncClient

    def _client(**kwargs):
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr("app.llm.openai_compat.httpx", SimpleNamespace(AsyncClient=_client))
    return seen


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _llm(mode: str) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://llm.test/v1",
        api_key="sk-test-only",
        model="test-model",
        response_format_mode=mode,
    )


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是语音笔记整理助手"},
        {"role": "user", "content": "请整理以下笔记"},
    ]


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# auto：400「不支持 response_format」→ 回落 json_object，进程内缓存生效
# ---------------------------------------------------------------------------


async def test_auto_falls_back_to_json_object_and_caches(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json=_DEEPSEEK_STYLE_400)
        return _ok('{"title": "t"}')

    seen = _install_transport(monkeypatch, handler)
    llm = _llm("auto")

    assert await llm.chat(_messages(), response_format=_SCHEMA) == '{"title": "t"}'
    assert calls["n"] == 2

    # 第 1 个请求：json_schema 变体（同原实现）
    assert _body(seen[0])["response_format"]["type"] == "json_schema"
    # 第 2 个请求：json_object 变体 + system 消息里带 schema 说明（含 "JSON"
    # 字样——DeepSeek 的 json_object 要求 prompt 中出现 json）
    second = _body(seen[1])
    assert second["response_format"] == {"type": "json_object"}
    assert second["messages"][0]["role"] == "system"
    assert "JSON" in second["messages"][0]["content"]
    assert json.dumps(_SCHEMA, ensure_ascii=False) in second["messages"][0]["content"]
    assert second["messages"][1] == _messages()[1]  # 原有消息保留

    # 再调一次：缓存生效，只发 1 个请求且直接 json_object
    await llm.chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 3
    assert _body(seen[2])["response_format"] == {"type": "json_object"}


async def test_auto_unrelated_400_raises_without_fallback_or_cache(monkeypatch):
    """与 response_format 无关的 400：照常抛 HTTPStatusError，不回落、不缓存。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid model name", "type": "invalid_request_error"}},
        )

    seen = _install_transport(monkeypatch, handler)
    llm = _llm("auto")

    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 1  # 只发 1 个请求

    # 未缓存：下一次仍是 json_schema 先行（再次只发 1 个请求）
    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 2
    assert _body(seen[1])["response_format"]["type"] == "json_schema"


async def test_auto_500_raises_without_fallback(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await _llm("auto").chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# json_object / json_schema 显式模式与无 schema 问答
# ---------------------------------------------------------------------------


async def test_json_object_mode_direct_schema_note_and_no_mutation(monkeypatch):
    seen = _install_transport(monkeypatch, lambda req: _ok('{"title": "t"}'))
    llm = _llm("json_object")

    # 没有 system 消息：在最前面插一条带 schema 说明的 system
    original = [{"role": "user", "content": "请整理以下笔记"}]
    snapshot = [dict(m) for m in original]
    assert await llm.chat(original, response_format=_SCHEMA) == '{"title": "t"}'
    assert original == snapshot  # 调用方传入的 messages 未被修改

    body = _body(seen[0])
    assert body["response_format"] == {"type": "json_object"}
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert "JSON" in msgs[0]["content"]
    assert json.dumps(_SCHEMA, ensure_ascii=False) in msgs[0]["content"]
    assert msgs[1] == original[0]

    # 已有 system 消息：说明追加在其内容末尾，不新增消息
    seen.clear()
    with_system = [{"role": "system", "content": "你是整理助手"}, {"role": "user", "content": "hi"}]
    with_system_snapshot = [dict(m) for m in with_system]
    await llm.chat(with_system, response_format=_SCHEMA)
    assert with_system == with_system_snapshot

    msgs2 = _body(seen[0])["messages"]
    assert len(msgs2) == 2
    assert msgs2[0]["role"] == "system"
    assert msgs2[0]["content"].startswith("你是整理助手")
    assert json.dumps(_SCHEMA, ensure_ascii=False) in msgs2[0]["content"]


async def test_json_schema_mode_400_raises_directly(monkeypatch):
    """显式 json_schema：供应商不支持时直接抛错，不回落、不缓存。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json=_DEEPSEEK_STYLE_400)

    seen = _install_transport(monkeypatch, handler)
    llm = _llm("json_schema")

    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 1
    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat(_messages(), response_format=_SCHEMA)
    assert calls["n"] == 2
    assert _body(seen[1])["response_format"]["type"] == "json_schema"


async def test_chat_without_response_format_omits_key(monkeypatch):
    """问答（response_format=None）：不发 response_format，行为不变。"""
    seen = _install_transport(monkeypatch, lambda req: _ok("你好"))
    assert await _llm("auto").chat([{"role": "user", "content": "hi"}]) == "你好"
    assert "response_format" not in _body(seen[0])


# ---------------------------------------------------------------------------
# 校验留在调用方：json_object 模式的坏输出照样在 _parse_llm_json 报错
# ---------------------------------------------------------------------------


async def test_json_object_bad_output_fails_extraction_parse(monkeypatch):
    from app.pipeline.extract import _parse_llm_json

    content = {"v": "模型抽风，这不是 JSON"}
    _install_transport(monkeypatch, lambda req: _ok(content["v"]))
    llm = _llm("json_object")

    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json(await llm.chat(_messages(), response_format=_SCHEMA))

    content["v"] = '[{"title": "数组不是对象"}]'  # 合法 JSON 但不是对象
    with pytest.raises(TypeError, match="not an object"):
        _parse_llm_json(await llm.chat(_messages(), response_format=_SCHEMA))


async def test_extract_json_object_bad_json_marks_note_failed(client, user, monkeypatch):
    """端到端：json_object 模式返回坏 JSON → 确定性失败，job 记 failed、
    笔记不被标整理完成（校验语义与 json_schema 模式完全同一套）。"""
    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    _install_transport(monkeypatch, lambda req: _ok("模型抽风，这不是 JSON"))

    async def fake_get_llm():
        return _llm("json_object")

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "会失败的笔记"},
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]
    await run_pending_jobs(retry_delay_seconds=0)

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()
    assert job.status == "failed"
    assert note.status == "failed"
    assert note.title is None  # 未被标为整理完成


async def test_extract_json_object_valid_json_organizes(client, user, monkeypatch):
    """端到端：json_object 模式返回合法 JSON → 笔记正常整理（title 写入）。"""
    from app.db import get_sessionmaker
    from app.pipeline.runner import run_pending_jobs

    content = json.dumps(
        {"title": "买火车票", "tags": ["出行"], "todos": [{"text": "买火车票"}], "suggested_reminders": []},
        ensure_ascii=False,
    )
    _install_transport(monkeypatch, lambda req: _ok(content))

    async def fake_get_llm():
        return _llm("json_object")

    monkeypatch.setattr("app.pipeline.extract.get_llm_client", fake_get_llm)

    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "明天去买火车票"},
        headers=auth_headers(user),
    )
    note_id = r.json()["note_id"]
    await run_pending_jobs(retry_delay_seconds=0)

    async with get_sessionmaker()() as session:
        note = await session.get(Note, note_id)
    assert note.status == "organized"
    assert note.title == "买火车票"
    assert note.llm_calls == 1


# ---------------------------------------------------------------------------
# 配置：LLM_RESPONSE_FORMAT 默认 auto，非法值启动即报错，工厂透传
# ---------------------------------------------------------------------------


def test_llm_response_format_settings():
    from app.config import Settings

    sqlite = "sqlite+aiosqlite:///./app.sqlite3"
    assert Settings(database_url=sqlite).llm_response_format == "auto"
    assert Settings(database_url=sqlite, llm_response_format="json_object").llm_response_format == (
        "json_object"
    )
    assert Settings(database_url=sqlite, llm_response_format="json_schema").llm_response_format == (
        "json_schema"
    )
    with pytest.raises(ValidationError, match="llm_response_format"):
        Settings(database_url=sqlite, llm_response_format="yolo")


def test_env_example_documents_llm_response_format():
    import pathlib

    env_example = (pathlib.Path(__file__).resolve().parent.parent / ".env.example").read_text(
        encoding="utf-8"
    )
    lines = [ln.strip() for ln in env_example.splitlines()]
    assert "LLM_RESPONSE_FORMAT=auto" in lines
    assert any("json_object" in ln for ln in lines)


async def test_get_llm_client_passes_response_format_mode(monkeypatch):
    from app.config import get_settings
    from app.llm.base import get_llm_client

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test-only")
    llm = await get_llm_client()
    assert isinstance(llm, OpenAICompatClient)
    assert llm.response_format_mode == "auto"
    monkeypatch.setattr(settings, "llm_response_format", "json_object")
    assert (await get_llm_client()).response_format_mode == "json_object"
