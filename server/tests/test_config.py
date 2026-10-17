"""jwt_secret 与记忆后端的生产校验：postgresql 下 secret 必须是 >=32
字节随机值的编码、MEMORY_BACKEND 不能是 inmemory，SQLite 不受影响。"""
import base64
import pathlib
import secrets

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_JWT_SECRET, ENV_EXAMPLE_JWT_PLACEHOLDER, Settings

PG_URL = "postgresql+asyncpg://u:p@localhost/db"
# 合法的固定 hex 形态：64 个 hex 字符 = 32 字节、32 种字节取值
GOOD_HEX = bytes(range(32)).hex()


def _pg_settings(**kwargs) -> Settings:
    """生产形态（postgresql）的合法基线：强 secret + mem0 记忆后端。

    不带 memory_backend 的话会先撞上“生产拒绝 inmemory”的校验（PG 跑
    测试时 conftest 还设置了 ALLOW_INMEMORY_IN_PROD，行为随运行方式
    漂移），显式固定住。
    """
    kwargs.setdefault("jwt_secret", GOOD_HEX)
    kwargs.setdefault("memory_backend", "mem0")
    return Settings(database_url=PG_URL, **kwargs)


def test_production_default_secret_rejected():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret=DEFAULT_JWT_SECRET)


def test_production_env_example_placeholder_rejected():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret=ENV_EXAMPLE_JWT_PLACEHOLDER)


def test_env_example_placeholder_matches_template():
    """ENV_EXAMPLE_JWT_PLACEHOLDER 必须与 server/.env.example 里的占位值一致。"""
    env_example = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
    for line in env_example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("JWT_SECRET="):
            assert line.split("=", 1)[1] == ENV_EXAMPLE_JWT_PLACEHOLDER
            return
    pytest.fail(".env.example 中未找到 JWT_SECRET 行")


def test_production_insecure_substring_rejected():
    for bad in ("a" * 40 + "-change-me", "Example" + "x" * 40, "PLACEHOLDER" + "y" * 40):
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            _pg_settings(jwt_secret=bad)


def test_production_human_readable_passwords_rejected():
    """人类可读口令必须被拒，即使长度/字符多样性看起来足够。

    第一条能按 base64url 解出 32 字节（结构检查会放行），靠口令字样拦下；
    第二条含 base64 字符集之外的 '!'，解码直接失败。
    """
    for bad in (
        "my-super-secret-jwt-key-for-production-2026",
        "Change_Me_To_A_Long_Random_Secret_123!",
    ):
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            _pg_settings(jwt_secret=bad)


def test_production_short_secret_rejected():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret="short")


def test_production_single_char_secret_rejected():
    # 长度够但字符多样性不足：暴力枚举空间太小
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret="a" * 40)


def test_production_token_urlsafe_secret_ok():
    secret = secrets.token_urlsafe(48)
    assert _pg_settings(jwt_secret=secret).jwt_secret == secret


def test_production_token_hex_secret_ok():
    # openssl rand -hex 32 / secrets.token_hex(32) 的形态
    secret = secrets.token_hex(32)
    assert _pg_settings(jwt_secret=secret).jwt_secret == secret


def test_production_std_base64_secret_ok():
    # openssl rand -base64 32 的形态（带 padding 的标准 base64）
    secret = base64.b64encode(secrets.token_bytes(32)).decode()
    assert _pg_settings(jwt_secret=secret).jwt_secret == secret


def test_production_fixed_strong_hex_ok():
    assert _pg_settings(jwt_secret=GOOD_HEX).jwt_secret == GOOD_HEX


def test_production_all_zero_hex_secret_rejected():
    # "00"*32 是 64 个 hex 字符（32 字节），但只有 1 种字节取值
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret="00" * 32)


def test_production_31_byte_hex_secret_rejected():
    # 62 个 hex 字符：既不满 hex 的 64 字符要求，也不允许被当成 base64 放行
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret=secrets.token_hex(31))


def test_production_undecodable_secret_rejected():
    # 不属于 hex / base64 / base64url 任何一种编码
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _pg_settings(jwt_secret="this is not an encoding!?" * 2)


def test_sqlite_default_secret_ok():
    # SQLite 开发/测试环境允许默认值，不因校验而启动失败
    # （显式传参以压过 conftest 设置的 JWT_SECRET 环境变量）
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", jwt_secret=DEFAULT_JWT_SECRET)
    assert settings.jwt_secret == DEFAULT_JWT_SECRET


def test_memory_backend_only_accepts_known_values():
    """P2：MEMORY_BACKEND 只接受 inmemory|mem0，未知值启动即校验失败。"""
    assert Settings(memory_backend="inmemory").memory_backend == "inmemory"
    assert Settings(memory_backend="mem0").memory_backend == "mem0"
    with pytest.raises(ValidationError, match="memory_backend"):
        Settings(memory_backend="redis")


def test_production_inmemory_memory_backend_rejected():
    """P1：生产（postgresql）+ inmemory 静默丢记忆，启动即拒绝。

    显式传 allow_inmemory_in_prod=False：PG 跑测试时 conftest 会把该
    环境变量设为 true，不压住的话这条用例在不同运行方式下行为漂移。
    """
    with pytest.raises(ValidationError, match="MEMORY_BACKEND"):
        _pg_settings(memory_backend="inmemory", allow_inmemory_in_prod=False)


def test_production_inmemory_with_explicit_test_flag_allowed():
    """ALLOW_INMEMORY_IN_PROD=true 只为跑 PG 测试套件放行（生产不要设）。"""
    settings = _pg_settings(memory_backend="inmemory", allow_inmemory_in_prod=True)
    assert settings.memory_backend == "inmemory"


def test_production_mem0_memory_backend_ok():
    assert _pg_settings(memory_backend="mem0").memory_backend == "mem0"


def test_sqlite_inmemory_memory_backend_ok():
    # SQLite 开发/测试环境不受生产校验影响
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", memory_backend="inmemory")
    assert settings.memory_backend == "inmemory"


def test_env_example_defaults_to_mem0_backend():
    """P1：部署模板默认 mem0（pgvector + OpenAI 兼容 embedding/LLM），
    inmemory 只留给单进程开发/测试。"""
    env_example = (
        pathlib.Path(__file__).resolve().parent.parent / ".env.example"
    ).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in env_example.splitlines()]
    assert "MEMORY_BACKEND=mem0" in lines
    # mem0 的 embedding/LLM OpenAI 兼容配置有占位说明
    assert any(ln.startswith("# MEM0_EMBEDDING_BASE_URL=") for ln in lines)
    assert any(ln.startswith("# MEM0_EMBEDDING_MODEL=") for ln in lines)


def test_get_memory_store_unknown_backend_raises(monkeypatch):
    """P2：即使绕过 config 校验，store 工厂也不静默退化成别的后端。"""
    from app.config import get_settings
    from app.memory import base as memory_base

    monkeypatch.setattr(memory_base, "_store", None)
    monkeypatch.setattr(get_settings(), "memory_backend", "bogus")
    with pytest.raises(ValueError, match="MEMORY_BACKEND"):
        memory_base.get_memory_store()


def test_job_lock_timeout_seconds_default_300():
    """P1-B：锁超时默认 300s（回收 running 任务的判定窗口）。"""
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3")
    assert settings.job_lock_timeout_seconds == 300
