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


async def test_patch_unchanged_text_with_stale_rev_409(client, user):
    """threads6 P1-2 / INV-11：内容不变（或缺 text）的快路径也必须先原子
    校验 rev——拿过期 rev 的无操作 PATCH 是 409，不是 200 + 过期视图；
    当前 rev 的无操作 PATCH 走 200 且不产生任何可观察变更。"""
    from sqlalchemy import func, select

    from app.db import get_sessionmaker
    from app.models import Job

    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes", json=_note_body("原始内容A"), headers=headers
    )
    note_id = created.json()["note_id"]

    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "新内容B", "rev": 1}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    current = patched.json()["rev"]
    assert current == 2

    # 过期 rev + 内容恰与当前一致（threads6 的精确场景）：409，不是 200
    stale_same = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "新内容B", "rev": 1}, headers=headers
    )
    assert stale_same.status_code == 409, stale_same.text
    assert stale_same.json()["error"]["code"] == "conflict"
    assert stale_same.json()["error"]["message"] == "版本冲突，请拉取最新版本后重试"

    # 过期 rev + 旧内容 / 缺 text：同样 409
    stale_old = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "原始内容A", "rev": 1}, headers=headers
    )
    assert stale_old.status_code == 409
    stale_no_text = await client.patch(f"/v1/notes/{note_id}", json={"rev": 1}, headers=headers)
    assert stale_no_text.status_code == 409

    # 当前 rev + 内容不变：200 快路径，rev/updated_at/队列全部不动
    before = await client.get(f"/v1/notes/{note_id}", headers=headers)
    maker = get_sessionmaker()
    async with maker() as session:
        jobs_before = (
            await session.execute(select(func.count()).select_from(Job).where(Job.note_id == note_id))
        ).scalar_one()

    noop = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "新内容B", "rev": current}, headers=headers
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["rev"] == current
    assert noop.json()["note"]["rev"] == current
    assert noop.json()["note"]["text"] == "新内容B"

    after = await client.get(f"/v1/notes/{note_id}", headers=headers)
    assert after.json()["rev"] == current
    assert after.json()["updated_at"] == before.json()["updated_at"], (
        "快路径不得推进 updated_at（自赋值 UPDATE 不产生可观察时间戳）"
    )
    async with maker() as session:
        jobs_after = (
            await session.execute(select(func.count()).select_from(Job).where(Job.note_id == note_id))
        ).scalar_one()
    assert jobs_after == jobs_before, "快路径不得入队新 job"


async def test_delete_note_soft_and_cascade(client, user, run_jobs):
    """软删除 + 记忆级联。INV-4：DELETE 请求本身**不碰** memory store
    （只置 deleted_at 并入队 memory_sync job）；级联清理由随后的
    memory_sync job 完成（delete_by_note）。"""
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
    # 请求路径不直接删记忆：DELETE 返回后记忆仍在
    remaining = [m for m in await store.list(user["user_id"]) if m.note_id == note_id]
    assert remaining, "DELETE 不应同步触碰 memory store（INV-4）"

    # 排空 job：extract 发现已删除 → done；memory_sync 对已删除笔记
    # 执行 delete_by_note 且不再 add
    await run_jobs()
    remaining = [m for m in await store.list(user["user_id"]) if m.note_id == note_id]
    assert remaining == [], "memory_sync 应清掉该笔记的全部记忆"
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


# ---------------------------------------------------------------------------
# QA（PR #2 复测）：正文含 NUL（\x00）在 schemas 校验层拒绝（422）。
# 不拦的话 PostgreSQL 文本列会直接编码报错（asyncpg → 500），NUL 流入
# LLM prompt / JSON 序列化也会出各种怪错。两库行为一致（校验在进库前）。
# ---------------------------------------------------------------------------


async def test_nul_rejected_in_note_strings_422(client, user):
    headers = auth_headers(user)
    nul = "含\x00的正文"
    cases = [
        ("text", nul),
        ("client_note_id", "cn\x00"),
        ("source", "app\x00"),
        ("timezone", "Asia/Shanghai\x00"),
        # asr 容器内任何层级的字符串（键与值）都要拦
        ("asr", {"transcript": nul}),
        ("asr", {"segments": [{"text": nul}]}),
        ("asr", {"ke\x00y": "value"}),
    ]
    for field, value in cases:
        body = {"client_note_id": str(uuid.uuid4()), "text": "正常正文", field: value}
        created = await client.post("/v1/notes", json=body, headers=headers)
        assert created.status_code == 422, (field, value, created.text)
        assert created.json()["error"]["code"] == "validation_error"

    # PATCH 的 text 同样拦截
    ok = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "正常长度"},
        headers=headers,
    )
    note_id, rev = ok.json()["note_id"], ok.json()["rev"]
    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": nul, "rev": rev}, headers=headers
    )
    assert patched.status_code == 422
    assert patched.json()["error"]["code"] == "validation_error"
