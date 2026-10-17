"""FastAPI 依赖：当前用户/设备解析。"""
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request

from app.auth import tokens
from app.db import get_sessionmaker
from app.errors import ApiError
from app.models import Device, User


@dataclass
class AuthContext:
    user_id: str
    device_id: str


async def get_auth(request: Request) -> AuthContext:
    """鉴权：解析 Bearer token 并校验用户/设备仍有效。

    校验在**独立短会话**里完成并立即归还连接（P2）：这个依赖原来挂着
    请求级会话（Depends(get_db_session)），鉴权后事务一直开到 handler
    结束——ask/search 的 LLM 与记忆检索（秒级外部调用）期间白白占着
    连接池。现在返回纯数据 AuthContext，业务处理按需另开短事务。
    """
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "missing bearer token")
    token = header[7:].strip()
    try:
        payload = tokens.decode_access_token(token)
    except jwt.InvalidTokenError:
        raise ApiError(401, "unauthorized", "invalid or expired token")
    user_id = payload.get("sub")
    device_id = payload.get("did")
    if not user_id or not device_id:
        raise ApiError(401, "unauthorized", "invalid token payload")

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        if user is None or user.status != "active":
            raise ApiError(401, "unauthorized", "user not active")
        device = await session.get(Device, device_id)
        if device is None or device.revoked_at is not None:
            raise ApiError(401, "unauthorized", "device revoked")
    return AuthContext(user_id=user_id, device_id=device_id)


async def get_current_user_id(auth: AuthContext = Depends(get_auth)) -> str:
    return auth.user_id
