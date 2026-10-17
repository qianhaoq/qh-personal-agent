"""并发安全：同 rev 并发 PATCH、同 refresh token 并发刷新、并发幂等创建。

SQLite 是单写者模型（库级写锁），两个并发请求在数据库层面实际被串行化，
因此这些测试在 SQLite 下验证的是**条件更新的语义**——即便两个请求都读到
相同的旧状态（旧 rev / 旧 refresh token / 同 client_note_id 不存在），
也恰好只有一个请求的 `UPDATE ... WHERE 旧值`（或唯一约束）能命中，另一
个得到 409/401/已存在记录，而不是双双成功或 500。

真实的读-写竞态需要 PostgreSQL 验证：设置环境变量 TEST_DATABASE_URL
（postgresql+asyncpg://...）后 conftest 会用 PG 运行同一套测试（含本
文件）；不设置则用 SQLite。
"""
import asyncio
import uuid

from tests.conftest import auth_headers


async def test_concurrent_patch_same_rev_exactly_one_success(client, user):
    """同 rev 并发 PATCH：恰好一个 200、一个 409。"""
    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "并发修改前的内容"},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    note_id, rev = r.json()["note_id"], r.json()["rev"]

    async def patch(text: str):
        return await client.patch(
            f"/v1/notes/{note_id}", json={"text": text, "rev": rev}, headers=headers
        )

    results = await asyncio.gather(patch("A 改成这样"), patch("B 改成那样"))
    codes = sorted(resp.status_code for resp in results)
    assert codes == [200, 409], [resp.status_code for resp in results]

    # 落库的是成功那一个的文本，rev 恰好 +1
    got = await client.get(f"/v1/notes/{note_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["rev"] == rev + 1
    assert got.json()["text"] in ("A 改成这样", "B 改成那样")


async def test_concurrent_refresh_same_token_at_most_one_success(client, user):
    """同 refresh token 并发刷新：最多一个 200（另一个 401 复用/失效）。"""
    refresh_token = user["refresh_token"]

    async def refresh():
        return await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})

    results = await asyncio.gather(refresh(), refresh())
    codes = [resp.status_code for resp in results]
    assert codes.count(200) <= 1, codes
    assert all(code in (200, 401) for code in codes), codes


async def test_concurrent_create_same_client_note_id_idempotent(client, user):
    """并发提交相同 client_note_id：两个都 202 且指向同一条笔记，不 500。"""
    headers = auth_headers(user)
    body = {"client_note_id": str(uuid.uuid4()), "text": "并发创建同一条"}

    async def create():
        return await client.post("/v1/notes", json=body, headers=headers)

    r1, r2 = await asyncio.gather(create(), create())
    assert r1.status_code == r2.status_code == 202, (r1.text, r2.text)
    assert r1.json()["note_id"] == r2.json()["note_id"]

    listing = await client.get("/v1/notes", headers=headers)
    assert len(listing.json()["items"]) == 1
