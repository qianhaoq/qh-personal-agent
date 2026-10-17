"""搜索与问答（MockLLM + InMemoryStore）。"""
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
