"""鉴权：登录、邀请码错误、401、refresh 轮换与复用吊销。"""

from tests.conftest import auth_headers


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
