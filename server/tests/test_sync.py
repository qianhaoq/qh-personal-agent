"""增量同步：/v1/sync 返回变化与删除。"""
import uuid

from tests.conftest import auth_headers


async def _create(client, user, text="明天早上九点提醒我交周报") -> str:
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": text},
        headers=auth_headers(user),
    )
    return r.json()["note_id"]


async def test_sync_returns_changes_since_cursor(client, user, run_jobs):
    first_id = await _create(client, user)
    r1 = await client.get("/v1/sync", headers=auth_headers(user))
    d1 = r1.json()
    assert d1["cursor"] > 0
    assert [n["id"] for n in d1["notes"]] == [first_id]
    assert d1["deleted"] == {"notes": [], "reminders": []}

    # 第二次用 cursor 拉：无新变化
    r2 = await client.get(f"/v1/sync?since={d1['cursor']}", headers=auth_headers(user))
    assert r2.json()["notes"] == []

    # 整理完成后：笔记变化 + 建议提醒出现
    await run_jobs()
    r3 = await client.get(f"/v1/sync?since={d1['cursor']}", headers=auth_headers(user))
    d3 = r3.json()
    assert d3["notes"][0]["status"] == "organized"
    assert any(n["id"] == first_id for n in d3["notes"])
    assert len(d3["reminders"]) >= 1
    assert d3["cursor"] > d1["cursor"]


async def test_sync_reports_deleted_ids(client, user, run_jobs):
    note_id = await _create(client, user)
    await run_jobs()
    await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))

    resp = await client.get("/v1/sync", headers=auth_headers(user))
    d = resp.json()
    assert note_id in d["deleted"]["notes"]
    assert all(n["id"] != note_id for n in d["notes"])


async def test_sync_isolated_per_user(client, user):
    from app.db import get_sessionmaker
    from tests.conftest import make_invite

    code2 = await make_invite(get_sessionmaker())
    r2 = await client.post(
        "/v1/auth/login", json={"invite_code": code2, "device_id": "dev_sync_other2"}
    )
    other_headers = {"authorization": f"Bearer {r2.json()['access_token']}"}

    await _create(client, user)
    resp = await client.get("/v1/sync", headers=other_headers)
    assert resp.json()["notes"] == []
