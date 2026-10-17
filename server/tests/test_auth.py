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


# ---------------------------------------------------------------------------
# threads6 P1-3 / INV-8 / INV-12：refresh 轮换 0 行后的锁内重读归因
# ---------------------------------------------------------------------------


async def test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke(
    client, invite_code, monkeypatch
):
    """threads6 P1-3：refresh 读到活行后、拿到 device 行锁之前，该 token 行
    被并发路径删除（第 6 次登录的上限修剪 / logout）→ 轮换 0 行。锁内重读
    归因：行不存在 → 普通 401，**不吊销设备**（挤出/登出的既定语义）；
    同设备其他 token 照常可用。旧行为在这里按重放吊销了整台设备。"""
    from sqlalchemy import delete as sa_delete

    from app.api import auth as auth_api
    from app.auth.tokens import hash_refresh_token
    from app.db import get_sessionmaker
    from app.models import Device, RefreshToken

    device_id = "dev_race_del"
    first = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": device_id}
    )
    second = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": device_id}
    )
    assert first.status_code == 200 and second.status_code == 200
    t1, t2 = first.json()["refresh_token"], second.json()["refresh_token"]

    real_lock_device = auth_api._lock_device

    async def lock_device_after_deleting_token(session, device_id):
        # 注入「读行之后、拿锁之前」窗口里发生的删除（同一事务内可见，
        # 与真实并发提交后的可见性等价）
        async with session.begin_nested():
            await session.execute(
                sa_delete(RefreshToken).where(
                    RefreshToken.token_hash == hash_refresh_token(t1)
                )
            )
        return await real_lock_device(session, device_id)

    monkeypatch.setattr(auth_api, "_lock_device", lock_device_after_deleting_token)

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": t1})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"

    # 设备未被吊销：另一枚 token 照常刷新
    other = await client.post("/v1/auth/refresh", json={"refresh_token": t2})
    assert other.status_code == 200, other.text
    async with get_sessionmaker()() as session:
        device = await session.get(Device, device_id)
    assert device.revoked_at is None


async def test_refresh_race_revoked_while_waiting_device_lock_detected_as_reuse(
    client, invite_code, monkeypatch
):
    """threads7 P1 / INV-12（auth.py:280）：同一枚 token 并发轮换，输家在
    「首读活行 → 拿到 device 锁」的窗口里被赢家作废 → 轮换 0 行。锁内重读
    必须绕过 identity map：轮换的条件 UPDATE 用 synchronize_session=False，
    不更新会话已加载的 row 对象——旧行为的 select(RefreshToken) 命中首读
    装载的旧对象，把并发轮换刚提交的 revoked_at 读成 None，重放被误判成
    「过期 401」、设备与赢家刚轮换到的新 token 全部保持活跃；正确行为是
    识别为重放 → 吊销整台设备。"""
    from sqlalchemy import update as sa_update

    from app.api import auth as auth_api
    from app.auth.tokens import hash_refresh_token
    from app.db import get_sessionmaker
    from app.models import Device, RefreshToken
    from app.timeutil import utcnow

    device_id = "dev_race_revoked"
    first = await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": device_id}
    )
    assert first.status_code == 200, first.text
    t1 = first.json()["refresh_token"]

    real_lock_device = auth_api._lock_device

    async def lock_device_after_concurrent_rotation(session, device_id):
        # 注入「读行之后、拿锁之前」窗口里赢家的轮换提交（同一事务内可见，
        # 与真实并发提交等价）。synchronize_session=False 是关键：真实并发
        # 发生在别的连接上、摸不到本会话的 identity map；若让注入的 UPDATE
        # 同步内存对象，会把要测的陈旧读 bug 掩盖掉
        async with session.begin_nested():
            await session.execute(
                sa_update(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(t1))
                .values(revoked_at=utcnow())
                .execution_options(synchronize_session=False)
            )
        return await real_lock_device(session, device_id)

    monkeypatch.setattr(auth_api, "_lock_device", lock_device_after_concurrent_rotation)

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": t1})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "refresh_reused", (
        "陈旧 identity map 读会把重放误判成「过期」普通 401"
    )

    async with get_sessionmaker()() as session:
        device = await session.get(Device, device_id)
    assert device.revoked_at is not None


@requires_postgres
async def test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg(
    client, invite_code
):
    """threads6 P1-3（PG 真并发）：设备已有 5 枚活 token，并发执行「第 6 次
    登录（上限修剪挤掉最老一枚）」与「用最老那枚刷新」。合法结果 ∈
    {401 unauthorized, 200}（刷新先拿 device 锁则轮换成功；登录先落定则行
    已删、锁内重读归因 401）——任何交错下都不得 refresh_reused、不得吊销
    设备，幸存的 token 必须仍可刷新。"""
    import asyncio

    from app.db import get_sessionmaker
    from app.models import Device

    device_id = "dev_evict_race"
    tokens = []
    for i in range(5):
        r = await client.post(
            "/v1/auth/login",
            json={"invite_code": invite_code, "device_id": device_id, "device_name": f"n{i}"},
        )
        assert r.status_code == 200, r.text
        tokens.append(r.json()["refresh_token"])

    refresh_task = client.post("/v1/auth/refresh", json={"refresh_token": tokens[0]})
    login_task = client.post(
        "/v1/auth/login",
        json={"invite_code": invite_code, "device_id": device_id, "device_name": "n5"},
    )
    refreshed, sixth = await asyncio.gather(refresh_task, login_task)

    assert sixth.status_code == 200, sixth.text
    assert refreshed.status_code in (200, 401), refreshed.text
    if refreshed.status_code == 401:
        assert refreshed.json()["error"]["code"] == "unauthorized", (
            "行被挤出后的刷新必须是普通 401，绝不能按重放吊销设备"
        )

    async with get_sessionmaker()() as session:
        device = await session.get(Device, device_id)
    assert device.revoked_at is None, "任何交错下设备都不得被吊销"

    # 幸存 token 可用：第 6 次登录返回的、以及中间那几枚
    for token in (sixth.json()["refresh_token"], tokens[2]):
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 200, (token, resp.status_code, resp.text)


async def test_refresh_prunes_expired_rows_keeps_revoked_until_expiry(client, invite_code):
    """threads6 P2 / INV-8：登录/轮换追加新行时顺手清掉该设备的已过期行
    （expires_at <= now，无论是否作废）；已作废但未过期的行保留到过期
    为止——有效期内重放仍触发 refresh_reused 吊销设备。"""
    from datetime import timedelta

    from app.auth.tokens import hash_refresh_token
    from app.db import get_sessionmaker
    from app.models import RefreshToken
    from app.timeutil import utcnow

    device_id = "dev_prune"

    async def _row(token: str) -> RefreshToken | None:
        async with get_sessionmaker()() as session:
            return (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.token_hash == hash_refresh_token(token)
                    )
                )
            ).scalar_one_or_none()

    t1 = (await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": device_id}
    )).json()["refresh_token"]
    # 直接把 t1 的行改成已过期（未作废）
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == hash_refresh_token(t1)
                )
            )
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(hours=1)
        await session.commit()

    # 登录路径的追加清理：过期行删除
    t2 = (await client.post(
        "/v1/auth/login", json={"invite_code": invite_code, "device_id": device_id}
    )).json()["refresh_token"]
    assert await _row(t1) is None, "登录追加时应清掉已过期行"

    # 轮换 t2：t2 的行作废但未过期 → 保留；t3 正常可用
    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": t2})
    assert rotated.status_code == 200, rotated.text
    t3 = rotated.json()["refresh_token"]
    t2_row = await _row(t2)
    assert t2_row is not None and t2_row.revoked_at is not None, "作废未过期的行必须保留"

    usable = await client.post("/v1/auth/refresh", json={"refresh_token": t3})
    assert usable.status_code == 200, usable.text

    # 保留的作废行仍承担重放检测：有效期内重放 t2 → 吊销设备
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": t2})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_reused"
