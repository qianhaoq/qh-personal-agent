"""用户数据隔离：A 读不到 B 的数据。"""
import uuid

from app.memory.base import get_memory_store


async def _new_user(client, invite_code, device_id):
    r = await client.post(
        "/v1/auth/login",
        json={"invite_code": invite_code, "device_id": device_id, "device_name": "phone"},
    )
    assert r.status_code == 200
    d = r.json()
    return d, {"authorization": f"Bearer {d['access_token']}"}


async def _make_invite(uses: int = 3) -> str:
    import secrets

    from app.db import get_sessionmaker
    from app.models import InviteCode

    code = secrets.token_hex(8)
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(InviteCode(code=code, max_uses=uses))
        await session.commit()
    return code


async def test_user_a_cannot_read_user_b_note(client, user):
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "A 的私密笔记"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    note_id = r.json()["note_id"]

    code_b = await _make_invite()
    _, headers_b = await _new_user(client, code_b, "dev_iso_b")

    # GET
    got = await client.get(f"/v1/notes/{note_id}", headers=headers_b)
    assert got.status_code == 404
    # PATCH
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "hack", "rev": 1}, headers=headers_b
    )
    assert patched.status_code == 404
    # DELETE
    deleted = await client.delete(f"/v1/notes/{note_id}", headers=headers_b)
    assert deleted.status_code == 404
    # LIST 看不到
    listing = await client.get("/v1/notes", headers=headers_b)
    assert all(n["id"] != note_id for n in listing.json()["items"])


async def test_memory_and_search_isolated(client, user, run_jobs):
    # A 整理一条笔记写入记忆
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "A 喜欢吃火锅"},
        headers={"authorization": f"Bearer {user['access_token']}"},
    )
    await run_jobs()

    code_b = await _make_invite()
    _, headers_b = await _new_user(client, code_b, "dev_iso_b2")

    # B 搜索同样的关键词，拿不到 A 的记忆
    resp = await client.get("/v1/search?q=火锅", headers=headers_b)
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    # B 提问同样的问题：没有相关记录
    ask = await client.post("/v1/ask", json={"text": "我喜欢吃什么"}, headers=headers_b)
    assert ask.status_code == 200
    assert ask.json()["answer"].startswith("我没有相关记录")
    assert ask.json()["citations"] == []

    # A 自己能搜到
    resp_a = await client.get(
        "/v1/search?q=火锅", headers={"authorization": f"Bearer {user['access_token']}"}
    )
    assert len(resp_a.json()["items"]) >= 1
    assert resp_a.json()["items"][0]["note_id"] == r.json()["note_id"]


async def test_delete_note_of_other_user_via_memory(client, user):
    """记忆删除也必须按 user_id 隔离（delete(ids) 只删属于自己的）。"""
    store = get_memory_store()
    (item_a,) = await store.add(user["user_id"], "A 的记忆", note_id="nte_a")

    code_b = await _make_invite()
    user_b, _ = await _new_user(client, code_b, "dev_iso_b3")

    deleted = await store.delete(user_b["user_id"], [item_a.id])
    assert deleted == 0
    assert len(await store.list(user["user_id"])) == 1
