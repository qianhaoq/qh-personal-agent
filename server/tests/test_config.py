"""jwt_secret 与记忆后端的生产校验：postgresql 下 secret 必须是 >=32
字节随机值的编码、MEMORY_BACKEND 不能是 inmemory（只能是 disabled），
SQLite 不受影响。"""
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
    """生产形态（postgresql）的合法基线：强 secret + disabled 记忆后端 +
    合法 LLM。

    不带 memory_backend 的话会先撞上“生产拒绝 inmemory”的校验（PG 跑
    测试时 conftest 还设置了 ALLOW_INMEMORY_IN_TESTS，行为随运行方式
    漂移），LLM 同理（ALLOW_MOCK_LLM_IN_PROD），显式固定住。
    """
    kwargs.setdefault("jwt_secret", GOOD_HEX)
    kwargs.setdefault("memory_backend", "disabled")
    kwargs.setdefault("allow_mock_llm_in_prod", True)
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
    """P2：MEMORY_BACKEND 只接受 inmemory|disabled，未知值（含已拆分的
    mem0——生产后端在 feat/mem0-backend 分支）启动即校验失败。"""
    assert Settings(memory_backend="inmemory").memory_backend == "inmemory"
    assert Settings(memory_backend="disabled").memory_backend == "disabled"
    with pytest.raises(ValidationError, match="memory_backend"):
        Settings(memory_backend="redis")
    # threads7 拆分：mem0 已随生产后端移出本仓，配置它必须报错而不是
    # 静默当别的后端跑
    with pytest.raises(ValidationError, match="memory_backend"):
        Settings(memory_backend="mem0")


def test_production_inmemory_memory_backend_rejected():
    """P1：生产（postgresql）+ inmemory 静默丢记忆，启动即拒绝。

    显式传 allow_inmemory_in_tests=False：PG 跑测试时 conftest 会把该
    环境变量设为 true，不压住的话这条用例在不同运行方式下行为漂移。
    """
    with pytest.raises(ValidationError, match="MEMORY_BACKEND"):
        _pg_settings(memory_backend="inmemory", allow_inmemory_in_tests=False)


def test_production_inmemory_rejection_mentions_mem0_backend_pr():
    """threads7 拆分：拒绝信息必须指出两条出路——生产后端在
    feat/mem0-backend，过渡期可显式 disabled（部署者按错误信息即可
    恢复部署，而不是对着报错猜）。"""
    with pytest.raises(ValidationError, match=r"feat/mem0-backend.*MEMORY_BACKEND=disabled"):
        _pg_settings(memory_backend="inmemory", allow_inmemory_in_tests=False)


def test_production_inmemory_with_explicit_test_flag_allowed():
    """ALLOW_INMEMORY_IN_TESTS=true 只为单进程跑 PG 测试套件放行（生产
    不要设；独立 worker 进程无论该标志都拒绝）。"""
    settings = _pg_settings(memory_backend="inmemory", allow_inmemory_in_tests=True)
    assert settings.memory_backend == "inmemory"


def test_production_disabled_memory_backend_ok():
    """threads7：disabled（显式空后端）在任何环境都允许——不需要放行
    标志（它是显式选择，不是要挡的危险配置）。"""
    settings = _pg_settings(memory_backend="disabled")
    assert settings.memory_backend == "disabled"


def test_sqlite_inmemory_memory_backend_ok():
    # SQLite 开发/测试环境不受生产校验影响
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", memory_backend="inmemory")
    assert settings.memory_backend == "inmemory"


def test_env_example_documents_memory_backend_choices():
    """threads7 拆分后：模板本地默认 inmemory（单进程局限写明）；
    disabled 以注释行给出；生产后端去向（feat/mem0-backend）与 disabled
    的过渡期语义写明；不再出现 MEM0_* 配置项（已随后端移出）。"""
    env_example = (
        pathlib.Path(__file__).resolve().parent.parent / ".env.example"
    ).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in env_example.splitlines()]
    assert "MEMORY_BACKEND=inmemory" in lines
    assert "# MEMORY_BACKEND=disabled" in lines
    # 单进程局限与生产后端去向必须写明
    assert any("单进程" in ln for ln in lines)
    assert any("feat/mem0-backend" in ln for ln in lines)
    # MEM0_* 配置已随生产后端移出本仓
    assert not [ln for ln in lines if ln.startswith("MEM0_")], "MEM0_* 已拆分至 feat/mem0-backend"
    assert not [ln for ln in lines if "requirements-mem0.txt" in ln]


def test_worker_refuses_inmemory_with_postgres():
    """threads5 P2-3 / INV-10.4：独立 worker 进程无论放行标志都拒绝
    inmemory+PG——它写的记忆对 api 进程的 /search 不可见；SQLite 本地
    开发与 disabled（显式空后端，threads7）不受限。"""
    from app.jobs.worker import check_memory_backend_shareable

    flagged = Settings(
        database_url=PG_URL,
        jwt_secret=GOOD_HEX,
        memory_backend="inmemory",
        allow_inmemory_in_tests=True,  # 即便放行标志开着
        allow_mock_llm_in_prod=True,
    )
    with pytest.raises(SystemExit, match="inmemory"):
        check_memory_backend_shareable(flagged)

    sqlite = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", memory_backend="inmemory")
    check_memory_backend_shareable(sqlite)  # SQLite 本地开发：不抛

    disabled = _pg_settings(memory_backend="disabled")
    check_memory_backend_shareable(disabled)  # PG + disabled（显式空后端）：不抛


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


# ---------------------------------------------------------------------------
# P2（Codex）：LLM 配置错误不得静默回落 MockLLM。
# - provider 只接受 mock|openai（拼错启动即报错）；
# - 生产（postgresql）：mock 拒绝（除非 ALLOW_MOCK_LLM_IN_PROD=true，
#   PG 测试套件用）、openai 缺 LLM_API_KEY 拒绝。
# ---------------------------------------------------------------------------


def test_llm_provider_only_accepts_known_values():
    # 显式 sqlite URL：隔离 PG 运行时环境变量对生产校验的影响
    sqlite = "sqlite+aiosqlite:///./app.sqlite3"
    assert Settings(database_url=sqlite, llm_provider="mock").llm_provider == "mock"
    # openai 属合法取值（三元组齐备校验由 INV-10 用例单独覆盖，这里配齐）
    assert (
        Settings(database_url=sqlite, **_OPENAI_COMPLETE).llm_provider == "openai"
    )
    with pytest.raises(ValidationError, match="llm_provider"):
        Settings(database_url=sqlite, llm_provider="openia")  # 拼错：直接校验失败


def test_production_mock_llm_rejected():
    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        _pg_settings(llm_provider="mock", allow_mock_llm_in_prod=False)


def test_production_mock_llm_allowed_with_test_flag():
    settings = _pg_settings(llm_provider="mock", allow_mock_llm_in_prod=True)
    assert settings.llm_provider == "mock"


def test_production_openai_without_api_key_rejected():
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        _pg_settings(
            llm_provider="openai",
            llm_api_key="",
            llm_base_url="https://llm.example.com/v1",
            llm_model="test-model",
        )


# ---------------------------------------------------------------------------
# INV-10（修 C3/C7）：LLM 三元组齐备校验与 DEFAULT_TIMEZONE 的 IANA 校验，
# 两者都在**任何环境**生效（不只是生产）。
# ---------------------------------------------------------------------------

_OPENAI_COMPLETE = {
    "llm_provider": "openai",
    "llm_api_key": "sk-test-only",
    "llm_base_url": "https://llm.example.com/v1",
    "llm_model": "test-model",
}


def test_openai_missing_api_key_rejected():
    kwargs = dict(_OPENAI_COMPLETE, llm_api_key="")
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", **kwargs)
    # 生产形态同样拒绝
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        _pg_settings(**kwargs)


def test_openai_missing_base_url_rejected():
    """修 C3：缺 LLM_BASE_URL 时相对 /chat/completions 的拼 URL 错必须在
    启动期暴露，而不是等到第一次 extract/ask 才失败。"""
    kwargs = dict(_OPENAI_COMPLETE, llm_base_url="")
    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", **kwargs)
    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        _pg_settings(**kwargs)


def test_openai_missing_model_rejected():
    kwargs = dict(_OPENAI_COMPLETE, llm_model="")
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", **kwargs)
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        _pg_settings(**kwargs)


def test_openai_missing_two_fields_lists_both():
    kwargs = dict(_OPENAI_COMPLETE, llm_base_url="", llm_model="")
    with pytest.raises(ValidationError, match="LLM_BASE_URL、LLM_MODEL"):
        Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", **kwargs)


def test_openai_all_three_configured_ok():
    settings = _pg_settings(**_OPENAI_COMPLETE)
    assert settings.llm_provider == "openai"
    assert settings.llm_base_url == "https://llm.example.com/v1"
    assert settings.llm_model == "test-model"


def test_default_timezone_invalid_rejected():
    """修 C7：DEFAULT_TIMEZONE 拼错会让 naive recorded_at 静默按错误时区
    解释、提醒解析回退——启动即拒。任何环境生效。"""
    for tz in ("Asia/Bejing", "UTC+8", ""):
        with pytest.raises(ValidationError, match="DEFAULT_TIMEZONE"):
            Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", default_timezone=tz)


def test_default_timezone_valid_ok():
    for tz in ("Asia/Shanghai", "UTC", "America/New_York"):
        settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", default_timezone=tz)
        assert settings.default_timezone == tz


def test_sqlite_mock_llm_ok():
    # SQLite 开发/测试环境不受生产校验影响
    settings = Settings(database_url="sqlite+aiosqlite:///./app.sqlite3", llm_provider="mock")
    assert settings.llm_provider == "mock"


async def test_get_llm_client_never_silently_falls_back(monkeypatch):
    """get_llm_client 运行时兜底：openai 缺 key、生产 mock 都抛错，不回落。"""
    from app.config import get_settings
    from app.llm.base import get_llm_client
    from app.llm.mock import MockLLM
    from app.llm.openai_compat import OpenAICompatClient

    settings = get_settings()

    # 默认（测试环境：mock，PG 套件 conftest 已置 ALLOW_MOCK_LLM_IN_PROD）
    assert isinstance(await get_llm_client(), MockLLM)

    # openai 配了 key：正常返回兼容客户端
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test-only")
    assert isinstance(await get_llm_client(), OpenAICompatClient)

    # openai 缺 key：抛错（任何环境），不再静默回落 MockLLM
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        await get_llm_client()

    # 生产形态 + mock 且未放行：抛错
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "database_url", PG_URL)
    monkeypatch.setattr(settings, "allow_mock_llm_in_prod", False)
    with pytest.raises(RuntimeError, match="ALLOW_MOCK_LLM_IN_PROD"):
        await get_llm_client()


# ---------------------------------------------------------------------------
# P2（审查）：生产默认值——/docs 默认关闭、CORS 默认不启用、日志级别可配。
# ---------------------------------------------------------------------------


def test_enable_docs_defaults_to_false():
    assert Settings(database_url="sqlite+aiosqlite:///./app.sqlite3").enable_docs is False


def test_cors_allow_origins_defaults_to_empty():
    assert Settings(database_url="sqlite+aiosqlite:///./app.sqlite3").cors_allow_origins == ""


def test_env_example_documents_new_runtime_flags():
    """部署模板要包含本轮新增的运行时配置项（占位不含真值）。"""
    env_example = (
        pathlib.Path(__file__).resolve().parent.parent / ".env.example"
    ).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in env_example.splitlines()]
    assert "ENABLE_DOCS=false" in lines
    assert "LOG_LEVEL=info" in lines
    assert any(ln.startswith("# CORS_ALLOW_ORIGINS=") for ln in lines)

