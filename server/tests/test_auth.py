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
# INV-8：device 原子 upsert + 每设备多枚 refresh token 的并发语义
# （PG 真并发独立连接；SQLite 单写者下请求天然串行，退化为同一语义
# 验证，故两后端都跑）。
# ---------------------------------------------------------------------------


async def test_concurrent_login_same_device_all_returned_tokens_refreshable(client, invite_code):
    """threads5 P1-2 / INV-8：同设备同账号 5 个并发登录全部 200（无
    IntegrityError 500），且**每个响应返回的 refresh token 都能成功
    refresh**——登录只追加行、互不覆盖，旧模型「后写者胜让其余 token
    天生无效」不再发生。"""
    import asyncio

    from sqlalchemy import func, select

    from app.db import get_sessionmaker
    from app.models import Device, RefreshToken

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
    tokens = [r.json()["refresh_token"] for r in results]
    assert len(set(tokens)) == 5, "并发登录应各自返回独立的 refresh token"

    # 每一枚都能成功轮换（200），无一枚被并发登录挤掉/覆盖
    for t in tokens:
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": t})
        assert resp.status_code == 200, (t, resp.status_code, resp.text)

    async with get_sessionmaker()() as session:
        devices = (
            await session.execute(select(Device).where(Device.id == "dev_conc"))
        ).scalars().all()
        assert len(devices) == 1 and devices[0].revoked_at is None
        live = (
            await session.execute(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.device_id == "dev_conc", RefreshToken.revoked_at.is_(None))
            )
        ).scalar_one()
    assert live == 5, f"5 枚轮换后的新 token 应全部存活（每设备上限 5）：{live}"


@requires_postgres
async def test_concurrent_login_same_device_linearizable_pg(client, invite_code):
    """INV-8（PG 独立会话真并发）：并发登录的写被 device 行/PK 串行化——
    库中恰一行 device、恰 5 行 refresh_tokens（=上限，未爆发超出上限的
    追加），且每枚 token 仍可轮换。"""
    import asyncio

    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Device, RefreshToken

    async def login(i: int):
        return await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": "dev_lin", "device_name": f"n{i}"},
        )

    results = await asyncio.gather(*[login(i) for i in range(5)])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    tokens = [r.json()["refresh_token"] for r in results]

    async with get_sessionmaker()() as session:
        devices = (
            await session.execute(select(Device).where(Device.id == "dev_lin"))
        ).scalars().all()
        rows = (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.device_id == "dev_lin", RefreshToken.revoked_at.is_(None)
                )
            )
        ).scalars().all()
    assert len(devices) == 1
    assert len(rows) == 5, f"并发追加应修剪到每设备 5 枚：{len(rows)}"
    for t in tokens:
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": t})
        assert resp.status_code == 200, (t, resp.status_code, resp.text)


async def test_refresh_token_cap_evicts_oldest_keeps_current(client, invite_code):
    """INV-8：每设备至多 5 枚未过期未作废 token——第 6 次登录挤出最老的
    一枚（其 refresh 401），本次返回的一定存活可用。"""
    from app.db import get_sessionmaker
    from app.models import RefreshToken

    tokens = []
    for i in range(6):
        resp = await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": "dev_cap", "device_name": f"n{i}"},
        )
        assert resp.status_code == 200, resp.text
        tokens.append(resp.json()["refresh_token"])

    evicted = await client.post("/v1/auth/refresh", json={"refresh_token": tokens[0]})
    assert evicted.status_code == 401, "最老的一枚应已被上限挤出（行已删，普通 401）"
    assert evicted.json()["error"]["code"] == "unauthorized"

    current = await client.post("/v1/auth/refresh", json={"refresh_token": tokens[5]})
    assert current.status_code == 200, current.text

    async with get_sessionmaker()() as session:
        live = (
            (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.device_id == "dev_cap", RefreshToken.revoked_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(live) == 5, f"修剪后应恰为 5 枚：{len(live)}"


async def test_refresh_rotation_revokes_only_rotated_token(client, invite_code):
    """INV-8：轮换只作废被轮换的那一枚——同设备的另一枚不受影响；重放
    已被轮换的 token 按复用吊销整台设备（原有语义）。"""
    from app.db import get_sessionmaker
    from app.models import Device

    first = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": "dev_rot"}
    )
    second = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": "dev_rot"}
    )
    t1, t2 = first.json()["refresh_token"], second.json()["refresh_token"]

    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": t1})
    assert rotated.status_code == 200, rotated.text
    t1_new = rotated.json()["refresh_token"]

    # 同设备另一枚（未被轮换）照常可用：轮换没有外溢
    other = await client.post("/v1/auth/refresh", json={"refresh_token": t2})
    assert other.status_code == 200, other.text

    # 重放被轮换的 t1 → 复用检测，吊销整台设备（原语义）
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": t1})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_reused"

    async with get_sessionmaker()() as session:
        device = await session.get(Device, "dev_rot")
    assert device.revoked_at is not None
    # 设备已吊销：剩余 token 全部失效
    for t in (t1_new, other.json()["refresh_token"]):
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": t})
        assert resp.status_code == 401, t


async def test_logout_revokes_all_device_refresh_tokens(client, invite_code):
    """INV-8：登出清除该设备全部 token——多枚并发登录留下的凭据一枚不剩。"""
    tokens = []
    access = None
    for i in range(3):
        resp = await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": "dev_out", "device_name": f"n{i}"},
        )
        assert resp.status_code == 200, resp.text
        tokens.append(resp.json()["refresh_token"])
        access = resp.json()["access_token"]

    logged_out = await client.post(
        "/v1/auth/logout", headers={"authorization": f"Bearer {access}"}
    )
    assert logged_out.status_code == 204, logged_out.text

    for t in tokens:
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": t})
        assert resp.status_code == 401, t
        assert resp.json()["error"]["code"] == "unauthorized"


async def test_refresh_expired_token_rejected_401(client, invite_code):
    """INV-8：30 天 TTL 落库强制——过期的 refresh 得 401，不再无限可用。"""
    from datetime import timedelta

    from sqlalchemy import update

    from app.db import get_sessionmaker
    from app.models import Device, RefreshToken
    from app.timeutil import utcnow

    login = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": "dev_exp_rt"}
    )
    token = login.json()["refresh_token"]
    device_id = "dev_exp_rt"

    # 把这枚 token 的行改成已过期（模拟 30 天后重放）
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.device_id == device_id, RefreshToken.revoked_at.is_(None))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    # 过期 ≠ 复用：设备不被吊销，重新登录可用
    async with maker() as session:
        device = await session.get(Device, device_id)
    assert device.revoked_at is None


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
