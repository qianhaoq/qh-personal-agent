"""鉴权接口：邀请码 + 设备注册、refresh 轮换、登出。

多 refresh token 模型（threads5 P1-2，INV-8）：凭据一行一枚存在
refresh_tokens 表——同设备同账号并发登录各自追加一行、互不覆盖，每个
200 响应返回的 refresh token 在过期 / 被显式作废（轮换、设备吊销、
登出）/ 被每设备 5 枚上限挤出之前都能用。轮换只作废被轮换的那一枚；
重放已作废的 token 按复用吊销整台设备（原有语义）。

锁序（INV-1）：device → refresh_tokens（login 经 device 原子 upsert 先
锁 device 行；refresh 先 PG FOR UPDATE device 行再条件更新 refresh_tokens），
不碰 sequences。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    refresh_expiry,
)
from app.common import new_id
from app.db import get_db_session
from app.deps import get_auth
from app.errors import ApiError
from app.jobs.queue import is_postgres
from app.models import Device, InviteCode, RefreshToken, User
from app.schemas import LoginRequest, RefreshRequest
from app.timeutil import as_utc, utcnow

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# 每台设备保留的未过期未作废 refresh token 上限（INV-8）：登录/轮换追加
# 新行后修剪最老的超限行；修剪条件排除本次返回的行——本次凭证一定幸存
_REFRESH_TOKENS_PER_DEVICE = 5


def _token_pair(user_id: str, device_id: str) -> dict:
    access, expires_in = create_access_token(user_id, device_id)
    return {
        "access_token": access,
        "refresh_token": new_refresh_token(),
        "expires_in": expires_in,
        "user_id": user_id,
    }


def _device_upsert(device_id: str, user_id: str, body: LoginRequest, now):
    """单条 INSERT … ON CONFLICT DO UPDATE（INV-8，修 C1）：原子完成
    「存在性检查 + 归属检查 + 复活/刷新」（不再写任何 refresh 哈希列——
    凭据在 refresh_tokens 表，见模块 docstring）。

    - 无冲突 → 插入新设备行；
    - 冲突且 user_id 相同 → DO UPDATE 清 revoked、刷新 last_seen
      （name/platform 仅在请求非空时覆盖）；
    - 冲突且 user_id 不同 → WHERE 不成立，rowcount=0 → 调用方复查后 403。

    并发语义：同设备登录在行/PK 上串行化（后面的 refresh_tokens 追加与
    修剪因此天然排在 device 行锁之后，无并发互删窗口）。
    """
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        insert_stmt = pg_insert(Device)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert_stmt = sqlite_insert(Device)
    stmt = insert_stmt.values(
        id=device_id,
        user_id=user_id,
        name=body.device_name or "",
        platform=body.platform or "android",
        last_seen=now,
    )
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=[Device.id],
        set_={
            "revoked_at": None,
            "last_seen": now,
            # 请求里为空的 name/platform 不覆盖已有值
            "name": func.coalesce(func.nullif(excluded.name, ""), Device.name),
            "platform": func.coalesce(func.nullif(excluded.platform, ""), Device.platform),
        },
        # 归属检查放进语句：设备已绑定其他账号时不更新（rowcount=0 → 403）
        where=Device.user_id == excluded.user_id,
    )


async def _append_refresh_token(
    session: AsyncSession, device_id: str, user_id: str, token: str, now
) -> None:
    """追加一枚 refresh token 并修剪最老的超限行（INV-8）。

    调用方已完成 device 行 upsert/加锁（锁序 device → refresh_tokens）。
    修剪条件排除本行、只删「未过期未作废」里最新的 4 枚之外的最老行：
    本次返回的 token 一定保留；并发登录在 device 行锁上串行化，修剪不会
    误删别的已提交响应的 token。
    """
    row = RefreshToken(
        id=new_id("rtk"),
        device_id=device_id,
        user_id=user_id,
        token_hash=hash_refresh_token(token),
        created_at=now,
        expires_at=refresh_expiry(),
    )
    session.add(row)
    await session.flush()
    live = (
        RefreshToken.device_id == device_id,
        RefreshToken.id != row.id,
        RefreshToken.revoked_at.is_(None),
        RefreshToken.expires_at > now,
    )
    keep = (
        select(RefreshToken.id)
        .where(*live)
        .order_by(RefreshToken.created_at.desc(), RefreshToken.id.desc())
        .limit(_REFRESH_TOKENS_PER_DEVICE - 1)
    )
    await session.execute(
        delete(RefreshToken)
        .where(*live, RefreshToken.id.not_in(keep))
        .execution_options(synchronize_session=False)
    )


async def _lock_device(session: AsyncSession, device_id: str) -> Device | None:
    """按锁序取 device 行：PG 下 FOR UPDATE 串行化同设备的轮换/吊销。"""
    stmt = select(Device).where(Device.id == device_id)
    if is_postgres():
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _revoke_device_for_reuse(session: AsyncSession, device_id: str):
    """重放已作废（被轮换/吊销）的 refresh：吊销设备并作废其全部 token
    （原有语义保留）。锁序：device → refresh_tokens。"""
    await session.rollback()
    device = await _lock_device(session, device_id)
    if device is not None:
        device.revoked_at = utcnow()
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.device_id == device_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    raise ApiError(401, "refresh_reused", "检测到 refresh token 复用，设备已吊销")


@router.post("/login")
async def login(body: LoginRequest, session: AsyncSession = Depends(get_db_session)):
    invite = await session.get(InviteCode, body.invite_code)
    now = utcnow()
    if invite is None or (invite.expires_at is not None and as_utc(invite.expires_at) <= now):
        raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")

    # 首次使用：创建账号（used 只计注册次数，之后同码登录复用同一账号）。
    # 首用抢占用条件 UPDATE 原子完成（只有把 owner_user_id 从 NULL 置为
    # 本事务用户 id 的事务才创建账号）：并发首登同一码时，其余事务
    # rowcount=0 后复查，按“同码后续登录复用同一账号”的既有语义登录
    # 先占到的人——同一邀请码不会创建出多个用户。加锁顺序上这是唯一
    # 要锁的业务行之一（invite → user → device → refresh_tokens，本接口
    # 不碰 sequences）
    user_id = invite.owner_user_id
    if user_id is None:
        if invite.used >= invite.max_uses:
            raise ApiError(403, "invalid_invite", "邀请码已用完")
        user_id = new_id("usr")
        claimed = await session.execute(
            update(InviteCode)
            .where(
                InviteCode.code == body.invite_code,
                InviteCode.owner_user_id.is_(None),
                InviteCode.used < InviteCode.max_uses,
                or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
            )
            .values(owner_user_id=user_id, used=InviteCode.used + 1)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount == 0:
            # 并发首登被别的事务抢走（或恰好过期/用尽）：回滚后重读已提交
            # 状态。owner 已有值 → 登录该 owner（保持既有语义，而不是 403：
            # 拿着同一个码的设备本就该落到同一个账号）；仍无 owner 或已
            # 过期 → 按无效邀请码处理
            await session.rollback()
            invite = await session.get(InviteCode, body.invite_code)
            if (
                invite is None
                or invite.owner_user_id is None
                or (invite.expires_at is not None and as_utc(invite.expires_at) <= now)
            ):
                raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")
            user_id = invite.owner_user_id
        else:
            session.add(User(id=user_id))
            await session.flush()

    result = _token_pair(user_id, body.device_id)
    upserted = await session.execute(_device_upsert(body.device_id, user_id, body, now))
    if upserted.rowcount == 0:
        # WHERE 不成立：设备已绑定其他账号。复查确认后 403（行不存在 =
        # 并发删除，当前无删除路径，保守按冲突处理并让部署问题暴露）
        await session.rollback()
        device = await session.get(Device, body.device_id)
        if device is None or device.user_id == user_id:
            raise ApiError(500, "internal_error", "设备行状态异常，登录未落定")
        raise ApiError(403, "device_conflict", "设备已绑定其他账号")
    # 登录只追加：本次返回的 token 与既有各行互不覆盖（INV-8）
    await _append_refresh_token(session, body.device_id, user_id, result["refresh_token"], now)
    await session.commit()
    return result


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_db_session)):
    h = hash_refresh_token(body.refresh_token)
    now = utcnow()
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == h))
    ).scalar_one_or_none()

    if row is not None:
        if row.revoked_at is not None:
            # 已被轮换/吊销的 token 重放：复用检测，吊销整台设备（原语义）
            return await _revoke_device_for_reuse(session, row.device_id)
        if as_utc(row.expires_at) <= now:
            # TTL 落库强制（README 已知限制中「有效期未强制」就此关闭）
            raise ApiError(401, "unauthorized", "refresh token 已过期")
        # 锁序（INV-1）：先锁 device 行（PG FOR UPDATE 串行化轮换与吊销、
        # 复查吊销态），再条件更新 refresh_tokens
        device = await _lock_device(session, row.device_id)
        if device is None or device.revoked_at is not None:
            raise ApiError(401, "unauthorized", "device revoked")
        # 轮换只作废被轮换的那一枚：条件 UPDATE 原子完成，同一枚 token
        # 并发刷新时只有一个能命中
        device_id, token_user_id = row.device_id, row.user_id
        result = _token_pair(token_user_id, device_id)
        rotated = await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == row.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        if rotated.rowcount == 1:
            device.last_seen = now
            await _append_refresh_token(session, device_id, token_user_id, result["refresh_token"], now)
            await session.commit()
            return result
        # rowcount==0 不能直接断定为重放（threads6 P1-3 / INV-8/INV-12）：
        # 首次读行（无锁）与拿到 device 行锁之间存在窗口，期间这行可能已被
        # 第 6 次登录的上限修剪或 logout 删除（挤出/登出的既定语义是普通
        # 401、不吊销设备），也可能恰好跨过 expires_at。锁内重读该行归因：
        current = (
            await session.execute(select(RefreshToken).where(RefreshToken.id == row.id))
        ).scalar_one_or_none()
        if current is None:
            # 行已不存在（被挤出/登出删除）：普通 401，不吊销设备
            raise ApiError(401, "unauthorized", "invalid refresh token")
        if current.revoked_at is None:
            # 未作废却没轮换成功：UPDATE 里的 now 是请求起始时刻，只可能是
            # 等锁期间跨过了过期时刻 → 按过期 401（不吊销设备）
            raise ApiError(401, "unauthorized", "refresh token 已过期")
        # revoked_at 非空：确系被轮换/吊销后的重放 → 复用检测吊销整台设备
        return await _revoke_device_for_reuse(session, device_id)

    raise ApiError(401, "unauthorized", "invalid refresh token")


@router.post("/logout", status_code=204)
async def logout(
    auth=Depends(get_auth), session: AsyncSession = Depends(get_db_session)
):
    # 登出：清除这台设备的全部 refresh 行（access 到期自然失效，不吊销
    # 设备行）。DELETE 而不是标 revoked：登出后的 token 重放按原有语义
    # 得普通 401（无法关联设备），不触发复用吊销
    device = await session.get(Device, auth.device_id)
    if device is not None:
        device.last_seen = utcnow()
        await session.execute(
            delete(RefreshToken)
            .where(RefreshToken.device_id == auth.device_id)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
