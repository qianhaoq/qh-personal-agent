"""鉴权接口：邀请码 + 设备注册、refresh 轮换、登出。"""
from fastapi import APIRouter, Depends
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from app.common import new_id
from app.db import get_db_session
from app.deps import get_auth
from app.errors import ApiError
from app.jobs.queue import is_postgres
from app.models import Device, InviteCode, User
from app.schemas import LoginRequest, RefreshRequest
from app.timeutil import as_utc, utcnow

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _token_pair(user_id: str, device_id: str) -> dict:
    access, expires_in = create_access_token(user_id, device_id)
    return {
        "access_token": access,
        "refresh_token": new_refresh_token(),
        "expires_in": expires_in,
        "user_id": user_id,
    }


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
    # 要锁的业务行之一（invite → user/device，本接口不碰 sequences）
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

    device = await session.get(Device, body.device_id)
    if device is not None and device.user_id != user_id:
        raise ApiError(403, "device_conflict", "设备已绑定其他账号")

    result = _token_pair(user_id, body.device_id)
    if device is None:
        device = Device(id=body.device_id, user_id=user_id)
        session.add(device)
    device.name = body.device_name or device.name
    device.platform = body.platform or device.platform
    device.refresh_token_hash = hash_refresh_token(result["refresh_token"])
    device.prev_refresh_token_hash = None
    device.revoked_at = None
    device.last_seen = now
    await session.commit()
    return result


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_db_session)):
    h = hash_refresh_token(body.refresh_token)
    current = (
        await session.execute(select(Device).where(Device.refresh_token_hash == h))
    ).scalar_one_or_none()

    if current is not None:
        if current.revoked_at is not None:
            raise ApiError(401, "unauthorized", "device revoked")
        # PG 下先锁行串行化轮换（锁后复查吊销态）；SQLite 无行锁，仅靠条件更新
        if is_postgres():
            locked = (
                await session.execute(
                    select(Device)
                    .where(Device.id == current.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if locked is not None and locked.revoked_at is not None:
                raise ApiError(401, "unauthorized", "device revoked")
        # 轮换本身用条件更新原子完成：同一枚 token 并发刷新时只有一个能命中
        result = _token_pair(current.user_id, current.id)
        rotated = await session.execute(
            update(Device)
            .where(
                Device.id == current.id,
                Device.refresh_token_hash == h,
                Device.revoked_at.is_(None),
            )
            .values(
                prev_refresh_token_hash=h,
                refresh_token_hash=hash_refresh_token(result["refresh_token"]),
                last_seen=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if rotated.rowcount == 1:
            await session.commit()
            return result
        # rowcount==0：token 已被并发请求换掉 → 落入下面的复用检测
        await session.rollback()

    # 复用检测：匹配到上一枚 refresh → 吊销设备
    previous = (
        await session.execute(select(Device).where(Device.prev_refresh_token_hash == h))
    ).scalar_one_or_none()
    if previous is not None:
        previous.revoked_at = utcnow()
        previous.refresh_token_hash = None
        previous.prev_refresh_token_hash = None
        await session.commit()
        raise ApiError(401, "refresh_reused", "检测到 refresh token 复用，设备已吊销")

    raise ApiError(401, "unauthorized", "invalid refresh token")


@router.post("/logout", status_code=204)
async def logout(
    auth=Depends(get_auth), session: AsyncSession = Depends(get_db_session)
):
    # 登出：清掉这台设备的 refresh（access 到期自然失效）
    device = await session.get(Device, auth.device_id)
    if device is not None:
        device.refresh_token_hash = None
        device.prev_refresh_token_hash = None
        device.last_seen = utcnow()
        await session.commit()
