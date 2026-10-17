"""搜索与问答（MockLLM + InMemoryStore）。"""
import asyncio
import uuid

from tests.conftest import auth_headers


async def _add_note(client, user, text) -> str:
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": text},
        headers=auth_headers(user),
    )
    return r.json()["note_id"]


async def test_search_finds_memory_after_organize(client, user, run_jobs):
    note_id = await _add_note(client, user, "我妈生日是三月五号，记得准备礼物")
    await run_jobs()

    resp = await client.get("/v1/search?q=妈妈生日", headers=auth_headers(user))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "整理后应能搜到记忆"
    top = items[0]
    assert top["note_id"] == note_id
    assert top["memory_id"]
    assert 0 < top["score"] <= 1
    assert "生日" in top["text"] or "三月五号" in top["text"]


async def test_search_no_result(client, user):
    resp = await client.get("/v1/search?q=完全不相关的东西", headers=auth_headers(user))
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_ask_with_memories(client, user, run_jobs):
    await _add_note(client, user, "我对花生过敏，吃饭要注意")
    await run_jobs()

    resp = await client.post(
        "/v1/ask", json={"text": "我吃东西要注意什么"}, headers=auth_headers(user)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"]
    assert not data["answer"].startswith("我没有相关记录")
    assert data["citations"], "应带引用"
    c = data["citations"][0]
    assert c["memory_id"] and "text" in c
    assert "花生" in c["text"]


async def test_ask_without_memories_does_not_fabricate(client, user):
    resp = await client.post(
        "/v1/ask", json={"text": "我家的 wifi 密码是多少"}, headers=auth_headers(user)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"].startswith("我没有相关记录")
    assert data["citations"] == []


async def test_ask_requires_auth(client):
    resp = await client.post("/v1/ask", json={"text": "hi"})
    assert resp.status_code == 401


async def test_ask_validation(client, user):
    resp = await client.post("/v1/ask", json={}, headers=auth_headers(user))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_ask_uses_client_time_as_reference(client, user, run_jobs, monkeypatch):
    """有 client_time/timezone 时，prompt 里的参照时间用它们而不是服务器时钟。"""
    await _add_note(client, user, "我对花生过敏，吃饭要注意")
    await run_jobs()

    captured: dict = {}

    class SpyLLM:
        async def chat(self, messages, response_format=None):
            captured["messages"] = messages
            return "根据你的记录：花生过敏"

    async def fake_get_llm():
        return SpyLLM()

    monkeypatch.setattr("app.api.ask.get_llm_client", fake_get_llm)
    resp = await client.post(
        "/v1/ask",
        json={
            "text": "我吃东西要注意什么",
            "client_time": "2026-10-16T08:00:00+00:00",
            "timezone": "Asia/Shanghai",
        },
        headers=auth_headers(user),
    )
    assert resp.status_code == 200
    system = captured["messages"][0]["content"]
    assert "2026-10-16T08:00:00+00:00" in system
    assert "Asia/Shanghai" in system


async def test_ask_bad_timezone_422(client, user):
    resp = await client.post(
        "/v1/ask",
        json={"text": "hi", "timezone": "Mars/Olympus"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 422


async def test_ask_naive_client_time_localized_by_timezone(client, user, run_jobs, monkeypatch):
    """P2：naive client_time 按请求时区解释，不再误当成 UTC。

    2026-10-17T09:00:00 + Asia/Shanghai → 2026-10-17T01:00:00Z；不带
    timezone 时用 settings.default_timezone（默认 Asia/Shanghai）。
    """
    await _add_note(client, user, "我对花生过敏，吃饭要注意")
    await run_jobs()

    captured: dict = {}

    class SpyLLM:
        async def chat(self, messages, response_format=None):
            captured["messages"] = messages
            return "根据你的记录：花生过敏"

    async def fake_get_llm():
        return SpyLLM()

    monkeypatch.setattr("app.api.ask.get_llm_client", fake_get_llm)
    resp = await client.post(
        "/v1/ask",
        json={
            "text": "我吃东西要注意什么",
            "client_time": "2026-10-17T09:00:00",
            "timezone": "Asia/Shanghai",
        },
        headers=auth_headers(user),
    )
    assert resp.status_code == 200
    assert "2026-10-17T01:00:00+00:00" in captured["messages"][0]["content"]

    # 不带 timezone：按 default_timezone 解释
    captured.clear()
    resp2 = await client.post(
        "/v1/ask",
        json={"text": "我吃东西要注意什么", "client_time": "2026-10-17T09:00:00"},
        headers=auth_headers(user),
    )
    assert resp2.status_code == 200
    assert "2026-10-17T01:00:00+00:00" in captured["messages"][0]["content"]


async def test_nul_rejected_in_ask_and_search_422(client, user):
    """QA（PR #2 复测）：ask 的 text / search 的 q 含 NUL 一律 422。"""
    headers = auth_headers(user)
    resp = await client.post(
        "/v1/ask", json={"text": "有问题\x00的提问", "timezone": "Asia/Shanghai\x00"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    search = await client.get("/v1/search", params={"q": "妈妈\x00生日"}, headers=headers)
    assert search.status_code == 422
    assert search.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# P2（Codex）：鉴权依赖不得把数据库会话/事务带进 LLM 与记忆检索。
#
# 旧实现 get_auth 挂着请求级会话（鉴权查询开启事务后一直不关），ask 的
# 秒级 LLM 调用期间白白占住连接池的一个连接。现在鉴权在独立短会话完成
# 并立即归还（app.deps.get_auth），ask/search 不再依赖请求级会话。
# ---------------------------------------------------------------------------


def _slow_ask_llm(monkeypatch, entered: asyncio.Event, release: asyncio.Event):
    class SlowLLM:
        async def chat(self, messages, response_format=None):
            entered.set()
            await release.wait()
            return "根据你的记录：慢慢答"

    async def fake_get_llm():
        return SlowLLM()

    monkeypatch.setattr("app.api.ask.get_llm_client", fake_get_llm)


async def test_ask_slow_llm_does_not_block_other_requests(client, user, run_jobs, monkeypatch):
    """ask 在慢 LLM 中等待时，另一个要访问数据库的已鉴权请求仍能完成。"""
    await _add_note(client, user, "我对花生过敏，吃饭要注意")  # 让 ask 有记忆可查
    await run_jobs()

    entered, release = asyncio.Event(), asyncio.Event()
    _slow_ask_llm(monkeypatch, entered, release)

    ask_task = asyncio.create_task(
        client.post("/v1/ask", json={"text": "我吃东西要注意什么"}, headers=auth_headers(user))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    other = await asyncio.wait_for(
        client.get("/v1/notes", headers=auth_headers(user)), timeout=3
    )
    assert other.status_code == 200, other.text

    release.set()
    resp = await asyncio.wait_for(ask_task, timeout=5)
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"]
