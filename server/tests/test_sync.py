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


async def _walk(client, headers, limit: int) -> tuple[dict[str, dict], list[dict]]:
    """按 cursor 用给定 limit 循环拉到 has_more=false。返回 (notes_by_id, reminders)。"""
    notes: dict[str, dict] = {}
    reminders: list[dict] = []
    cursor = 0
    for _ in range(200):  # 上限保护，has_more 一直为 true 时不要死循环
        resp = await client.get(
            f"/v1/sync?since={cursor}&limit={limit}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        page_size = (
            len(page["notes"])
            + len(page["reminders"])
            + len(page["deleted"]["notes"])
            + len(page["deleted"]["reminders"])
        )
        assert page_size <= limit, f"单页条数 {page_size} 超过 limit={limit}"
        for n in page["notes"]:
            notes[n["id"]] = n
        for nid in page["deleted"]["notes"]:
            notes.setdefault(nid, {"id": nid})
        reminders.extend(page["reminders"])
        reminders.extend({"id": rid} for rid in page["deleted"]["reminders"])
        if page["cursor"] > cursor:
            cursor = page["cursor"]
        if not page["has_more"]:
            return notes, reminders
    raise AssertionError("分页未在 200 页内结束")


async def test_sync_pagination_merged_stream_no_loss(client, user, run_jobs):
    """notes/reminders 合并流分页：小 limit 循环拉取不漏数据。

    3 条普通笔记 + 1 条“明天上午十点交周报”，整理后 notes 与 reminders
    交错出现在同一流里；按 limit=2 / limit=1 续拉必须拿全 4 条笔记和建议提醒。
    """
    headers = auth_headers(user)
    plain = ["今天天气不错随手一记", "中午吃了一碗面", "下班路上听了个播客"]
    note_ids = []
    for text in plain:
        note_ids.append(await _create(client, user, text))
    reminder_note_id = await _create(client, user, "明天上午十点交周报")
    await run_jobs()

    for limit in (2, 1):
        notes, reminders = await _walk(client, headers, limit)
        collected = set(notes)
        expected = set(note_ids) | {reminder_note_id}
        assert collected == expected, f"limit={limit} 期望 {expected}，实际 {collected}"
        suggested = [r for r in reminders if r.get("status") == "suggested"]
        assert suggested, f"limit={limit} 未拉到建议提醒"
        assert all(r["note_id"] == reminder_note_id for r in suggested)


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
