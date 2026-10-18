"""JWT 签发/校验与 refresh token 哈希。"""
import hashlib
from datetime import UTC, datetime, timedelta

import jwt

from app.common import new_token
from app.config import get_settings

_ALGO = "HS256"


def hash_refresh_token(token: str) -> str:
    """服务端只存 refresh 哈希。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    user_id: str, device_id: str, credential_generation: int
) -> tuple[str, int]:
    """gen claim 携带签发时的设备凭证代（INV-8 陈述 4，threads12 P1）：
    get_auth 校验它与 devices.credential_generation 一致——吊销后同设备
    再登录（复活 +1 代），吊销前签发的 access 不得随之回魂。"""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.access_token_ttl_seconds
    payload = {
        "sub": user_id,
        "did": device_id,
        "gen": credential_generation,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)
    return token, expires_in


def decode_access_token(token: str) -> dict:
    """校验签名与过期。失败抛 jwt.InvalidTokenError。"""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> str:
    """256 位随机 refresh token（只返回明文一次）。"""
    return new_token(32)


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=get_settings().refresh_token_ttl_days)
