"""鉴权：登录、邀请码错误、401、refresh 轮换与复用吊销。"""

from sqlalchemy import select

from tests.conftest import auth_headers, requires_postgres


async def test_login_success(client, invite_code):
    resp = await client.post(
        "/v1/auth/login",
        json={"invite_code": invite_code, "device_id": "dev_ok_1", "device_name": "phone"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] and data["refresh_token"]
    assert data["expires_in"] == 15 * 60
    assert data["user_id"].startswith("usr_")


async def test_login_same_invite_returns_same_user(client, invite_code):
    r1 = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": "d1"}
    )
    r2 = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": "d2"}
    )
    assert r1.json()["user_id"] == r2.json()["user_id"]


async def test_login_wrong_invite(client):
    resp = await client.post(
        "/v1/auth/login",
        json={"invite_code": "nope", "device_id": "dev_x", "device_name": "phone"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "invalid_invite"


async def test_business_endpoint_without_token_401(client):
    cases = [
        ("get", "/v1/notes", {}),
        ("get", "/v1/sync", {}),
        ("get", "/v1/search?q=x", {}),
        ("post", "/v1/ask", {"json": {"text": "hi"}}),
    ]
    for method, path, kwargs in cases:
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, (method, path, resp.status_code)
        assert resp.json()["error"]["code"] == "unauthorized"


async def test_invalid_token_401(client):
    resp = await client.get("/v1/notes", headers={"authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


async def test_refresh_rotation_and_reuse_revokes_device(client, user):
    old_refresh = user["refresh_token"]

    # 第一次刷新：成功且换新
    r1 = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r1.status_code == 200
    new_refresh = r1.json()["refresh_token"]
    assert new_refresh != old_refresh
    assert r1.json()["user_id"] == user["user_id"]

    # 复用旧 refresh：吊销设备
    r2 = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "refresh_reused"

    # 设备已吊销：新 refresh 也失效，access 也失效
    r3 = await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert r3.status_code == 401
    resp = await client.get("/v1/notes", headers=auth_headers(user))
    assert resp.status_code == 401


async def test_logout_clears_refresh(client, user):
    resp = await client.post("/v1/auth/logout", headers=auth_headers(user))
    assert resp.status_code == 204
    r = await client.post("/v1/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert r.status_code == 401


async def test_refresh_garbage_token_401(client):
    r = await client.post("/v1/auth/refresh", json={"refresh_token": "garbage"})
    assert r.status_code == 401


async def test_expired_invite_rejected(client):
    import secrets
    from datetime import timedelta

    from app.db import get_sessionmaker
    from app.models import InviteCode
    from app.timeutil import utcnow

    code = secrets.token_hex(4)
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(InviteCode(code=code, max_uses=1, expires_at=utcnow() - timedelta(hours=1)))
        await session.commit()
    resp = await client.post("/v1/auth/login", json={"invite_code": code, "device_id": "d_exp"})
    assert resp.status_code == 403


async def test_login_device_bound_to_other_user_403(client, invite_code):
    """INV-8：device 已绑定其他账号 → 403 device_conflict（原子 upsert 的
    WHERE 不成立 → rowcount=0 → 复查后拒绝），且原账号的 refresh 不被覆写。"""
    import secrets

    from app.db import get_sessionmaker
    from app.models import Device, InviteCode

    first = await client.post(
        "/v1/auth/login",
        json={"invite_code": invite_code, "device_id": "dev_bound", "device_name": "A 的手机"},
    )
    assert first.status_code == 200
    user_a, refresh_a = first.json()["user_id"], first.json()["refresh_token"]

    # 同一设备换邀请码（= 其他账号）登录
    code_b = secrets.token_hex(8)
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(InviteCode(code=code_b, max_uses=1))
        await session.commit()
    second = await client.post(
        "/v1/auth/login", json={"invite_code": code_b, "device_id": "dev_bound"}
    )
    assert second.status_code == 403, second.text
    assert second.json()["error"]["code"] == "device_conflict"

    # 原账号的 refresh 未被覆写，仍可正常轮换
    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_a})
    assert refreshed.status_code == 200
    async with maker() as session:
        device = await session.get(Device, "dev_bound")
    assert device.user_id == user_a


async def test_nul_rejected_in_auth_strings_422(client, invite_code):
    """QA（PR #2 复测）：鉴权相关字符串入参含 NUL 一律 422（schemas 层拦截）。"""
    for field, value in (
        ("invite_code", f"{invite_code}\x00"),
        ("device_id", "dev\x00"),
        ("device_name", "phone\x00"),
        ("platform", "android\x00"),
    ):
        resp = await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": "dev_nul", field: value},
        )
        assert resp.status_code == 422, (field, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"

    refresh = await client.post("/v1/auth/refresh", json={"refresh_token": "tok\x00en"})
    assert refresh.status_code == 422
    assert refresh.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# INV-8：device 原子 upsert 的并发语义（PG 真并发；SQLite 单写者下请求
# 天然串行，退化为同一语义验证，故两后端都跑）。
# ---------------------------------------------------------------------------


async def test_concurrent_login_same_device_all_ok_no_500(client, invite_code):
    """同设备同账号 5 个并发登录：全部 200（无 IntegrityError 500），
    库中恰一行、refresh hash 与其中一枚返回的 token 一致（后写者胜）。"""
    import asyncio

    from app.auth.tokens import hash_refresh_token
    from app.db import get_sessionmaker
    from app.models import Device

    results = await asyncio.gather(
        *[
            client.post(
                "/v1/auth/login",
                json={"invite_code": invite_code, "device_id": "dev_conc", "device_name": "phone"},
            )
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    tokens = {r.json()["refresh_token"] for r in results}

    async with get_sessionmaker()() as session:
        device = await session.get(Device, "dev_conc")
    assert device is not None
    assert device.revoked_at is None and device.prev_refresh_token_hash is None
    assert device.refresh_token_hash in {hash_refresh_token(t) for t in tokens}, (
        "库中 hash 必须与最后落定的那次登录一致（后写者胜）"
    )


@requires_postgres
async def test_concurrent_login_same_device_linearizable_pg(client, invite_code):
    """INV-8（PG 独立会话真并发）：并发登录的写被 device 行/PK 串行化——
    库中恰一行，最终 hash 恰好等于**最后完成**那次登录的 refresh hash。"""
    import asyncio

    from sqlalchemy import select

    from app.auth.tokens import hash_refresh_token
    from app.db import get_sessionmaker
    from app.models import Device

    order: list[str] = []  # 各请求完成的先后（响应返回顺序）

    async def login(i: int):
        resp = await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": "dev_lin", "device_name": f"n{i}"},
        )
        order.append(resp.json().get("refresh_token", ""))
        return resp

    results = await asyncio.gather(*[login(i) for i in range(5)])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len(order) == 5

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(Device).where(Device.id == "dev_lin"))
        ).scalars().all()
    assert len(rows) == 1
    # 线可串行化：库中 hash = 某个已提交登录的 hash（后写者胜）
    assert rows[0].refresh_token_hash in {hash_refresh_token(t) for t in order}


@requires_postgres
async def test_concurrent_first_login_same_device_no_integrity_error(client):
    """INV-8：并发首登同设备（均无行）由 PK 冲突 upsert 原子解决——不再
    出现旧行为的 IntegrityError 500。仅 PG（SQLite 写天然串行无此窗口）。"""
    import asyncio
    import secrets

    from app.db import get_sessionmaker
    from app.models import Device, InviteCode

    code = secrets.token_hex(8)
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(InviteCode(code=code, max_uses=5))
        await session.commit()

    results = await asyncio.gather(
        *[
            client.post(
                "/v1/auth/login",
                json={"invite_code": code, "device_id": "dev_first", "device_name": "race"},
            )
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]

    async with maker() as session:
        rows = (
            await session.execute(select(Device).where(Device.id == "dev_first"))
        ).scalars().all()
    assert len(rows) == 1
