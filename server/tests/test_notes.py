"""笔记：幂等创建、乐观锁 409、软删除与记忆级联。"""
import uuid

from tests.conftest import auth_headers


def _note_body(text: str = "明天下午三点提醒我给张总回电话") -> dict:
    return {"client_note_id": str(uuid.uuid4()), "text": text, "source": "app"}


async def test_create_note_202_and_idempotent(client, user):
    body = _note_body()
    r1 = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert r1.status_code == 202, r1.text
    d1 = r1.json()
    assert d1["status"] == "uploaded"
    assert d1["rev"] == 1

    # 同一个 client_note_id 再提交：返回同一条，不新建
    r2 = await client.post("/v1/notes", json=body, headers=auth_headers(user))
    assert r2.status_code == 202
    d2 = r2.json()
    assert d2["note_id"] == d1["note_id"]

    listing = await client.get("/v1/notes", headers=auth_headers(user))
    assert len(listing.json()["items"]) == 1


async def test_get_note(client, user):
    r = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id = r.json()["note_id"]
    resp = await client.get(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert resp.status_code == 200
    assert resp.json()["text"].startswith("明天下午三点")


async def test_get_missing_note_404(client, user):
    resp = await client.get("/v1/notes/nte_nothing", headers=auth_headers(user))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_patch_optimistic_lock_409(client, user):
    r = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id, rev = r.json()["note_id"], r.json()["rev"]

    # 用旧 rev 修改 → 409
    conflict = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "改成别的", "rev": rev}, headers=auth_headers(user)
    )
    assert conflict.status_code == 200  # 第一次用正确 rev 成功

    stale = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "再改", "rev": rev}, headers=auth_headers(user)
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"

    got = await client.get(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert got.json()["rev"] == rev + 1
    assert got.json()["status"] == "uploaded"  # 修改后回到 uploaded 等待重新整理


async def test_delete_note_soft_and_cascade(client, user):
    from app.memory.base import get_memory_store

    r = await client.post("/v1/notes", json=_note_body(), headers=auth_headers(user))
    note_id = r.json()["note_id"]
    store = get_memory_store()
    await store.add(user["user_id"], "待删除的记忆", note_id=note_id)

    resp = await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert resp.status_code == 204

    # 读取 → 404（软删除）
    got = await client.get(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert got.status_code == 404
    # 关联记忆被删除
    remaining = [m for m in await store.list(user["user_id"]) if m.note_id == note_id]
    assert remaining == []
    # 再次删除 → 404
    again = await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))
    assert again.status_code == 404


async def test_notes_pagination(client, user):
    for i in range(5):
        body = _note_body(f"第 {i} 条笔记内容")
        await client.post("/v1/notes", json=body, headers=auth_headers(user))
    page1 = await client.get("/v1/notes?limit=3", headers=auth_headers(user))
    d1 = page1.json()
    assert len(d1["items"]) == 3
    assert d1["next_cursor"] is not None
    page2 = await client.get(
        f"/v1/notes?limit=3&cursor={d1['next_cursor']}", headers=auth_headers(user)
    )
    assert len(page2.json()["items"]) == 2


async def test_create_note_timezone_defaults_to_settings(client, user, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "default_timezone", "Asia/Tokyo")
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "没带时区"},
        headers=auth_headers(user),
    )
    assert r.status_code == 202
    got = await client.get(f"/v1/notes/{r.json()['note_id']}", headers=auth_headers(user))
    assert got.json()["timezone"] == "Asia/Tokyo"


async def test_create_note_naive_recorded_at_localized(client, user):
    """P2：naive recorded_at 按笔记时区（缺省 default_timezone）解释成 UTC。

    2026-10-17T09:00:00 + Asia/Shanghai → 2026-10-17T01:00:00Z。
    """
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "随手记",
            "recorded_at": "2026-10-17T09:00:00",
            "timezone": "Asia/Shanghai",
        },
        headers=auth_headers(user),
    )
    assert r.status_code == 202, r.text
    got = await client.get(f"/v1/notes/{r.json()['note_id']}", headers=auth_headers(user))
    assert got.json()["recorded_at"] == "2026-10-17T01:00:00+00:00"

    # 不带 timezone：退回 settings.default_timezone（默认 Asia/Shanghai）
    r2 = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "随手记二",
            "recorded_at": "2026-10-17T09:00:00",
        },
        headers=auth_headers(user),
    )
    assert r2.status_code == 202
    got2 = await client.get(f"/v1/notes/{r2.json()['note_id']}", headers=auth_headers(user))
    assert got2.json()["recorded_at"] == "2026-10-17T01:00:00+00:00"


async def test_create_note_bad_timezone_422(client, user):
    for tz in ("Mars/Olympus", "UTC+8", "x" * 65):
        r = await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "x", "timezone": tz},
            headers=auth_headers(user),
        )
        assert r.status_code == 422, (tz, r.status_code)
        assert r.json()["error"]["code"] == "validation_error"


async def test_create_note_asr_too_large_422(client, user):
    r = await client.post(
        "/v1/notes",
        json={
            "client_note_id": str(uuid.uuid4()),
            "text": "x",
            "asr": {"transcript": "y" * 5000},
        },
        headers=auth_headers(user),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_note_text_too_long_422(client, user):
    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "x" * 5001},
        headers=auth_headers(user),
    )
    assert created.status_code == 422

    ok = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "正常长度"},
        headers=auth_headers(user),
    )
    note_id, rev = ok.json()["note_id"], ok.json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}",
        json={"text": "y" * 5001, "rev": rev},
        headers=auth_headers(user),
    )
    assert patched.status_code == 422
