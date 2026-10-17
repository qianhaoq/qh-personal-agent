"""jwt_secret 生产校验：postgresql 下默认值/过短即拒绝，SQLite 不受影响。"""
import pytest
from pydantic import ValidationError

from app.config import DEFAULT_JWT_SECRET, Settings

PG_URL = "postgresql+asyncpg://u:p@localhost/db"
GOOD_SECRET = "x" * 32  # 满足 HS256 建议的最短长度


def test_production_default_secret_rejected():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(database_url=PG_URL, jwt_secret=DEFAULT_JWT_SECRET)


def test_production_short_secret_rejected():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(database_url=PG_URL, jwt_secret="short")


def test_production_strong_secret_ok():
    assert Settings(database_url=PG_URL, jwt_secret=GOOD_SECRET).jwt_secret == GOOD_SECRET


def test_sqlite_default_secret_ok():
    # SQLite 开发/测试环境允许默认值，不因校验而启动失败
    # （显式传参以压过 conftest 设置的 JWT_SECRET 环境变量）
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", jwt_secret=DEFAULT_JWT_SECRET)
    assert settings.jwt_secret == DEFAULT_JWT_SECRET
