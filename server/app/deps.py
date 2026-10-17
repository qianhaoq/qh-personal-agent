"""FastAPI 依赖：当前用户/设备解析。"""
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import tokens
from app.db import get_db_session
from app.errors import ApiError
from app.models import Device, User


@dataclass
class AuthContext:
    user_id: str
    device_id: str


async def get_auth(request: Request, session: AsyncSession = Depends(get_db_session)) -> AuthContext:
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

    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise ApiError(401, "unauthorized", "user not active")
    device = await session.get(Device, device_id)
    if device is None or device.revoked_at is not None:
        raise ApiError(401, "unauthorized", "device revoked")
    return AuthContext(user_id=user_id, device_id=device_id)


async def get_current_user_id(auth: AuthContext = Depends(get_auth)) -> str:
    return auth.user_id
