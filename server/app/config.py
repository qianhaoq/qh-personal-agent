"""应用配置：全部来自环境变量（.env 亦可）。"""
import base64
import binascii
import string
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 明显的占位符，只允许配合 SQLite 开发/测试使用
DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"
# .env.example 里的占位值（测试会读取 .env.example 校验与此常量一致）
ENV_EXAMPLE_JWT_PLACEHOLDER = "change-me-to-a-long-random-secret"
# 明显的占位/人类可读口令字样，出现在生产密钥里即拒绝（大小写不敏感）。
# secret/password 用来挡人类可读口令（如 my-super-secret-...）：随机编码里
# 出现连续 6/8 个字母单词的概率约 1e-9（hex 编码里不可能出现）
_INSECURE_SUBSTRINGS = ("change-me", "changeme", "example", "placeholder", "secret", "password")
# 生产密钥的结构要求：>=32 字节随机值的编码（hex 或 base64/base64url）
_MIN_SECRET_BYTES = 32
_MIN_HEX_CHARS = 64  # 32 字节 = 64 个 hex 字符
# 解码后不同字节取值数的下限（防 "00"*32 这类低熵值只靠长度过关）
_MIN_SECRET_DISTINCT_BYTES = 16
_HEX_DIGITS = frozenset(string.hexdigits)
# base64url 的 -_ 换成标准 base64 的 +/ 后统一走严格的标准解码
_URLSAFE_TO_STD = str.maketrans("-_", "+/")


def _decode_secret(secret: str) -> bytes | None:
    """把 JWT_SECRET 解码成它编码的随机字节；不是合法编码返回 None。

    - 全部字符都是 hex 数字时按 hex 处理：需 >=64 个字符（解码 >=32 字节）
      且长度为偶数；不满足直接拒绝，不回退按 base64 解释——否则 31 字节
      的 hex（62 个字符）会被当成 46 字节的 base64 放行，绕过长度要求
    - 其余按 base64 / base64url 处理（允许无 padding），严格校验字符集
    """
    if all(c in _HEX_DIGITS for c in secret):
        if len(secret) < _MIN_HEX_CHARS or len(secret) % 2:
            return None
        return bytes.fromhex(secret)
    padded = secret + "=" * (-len(secret) % 4)
    for candidate in (padded, padded.translate(_URLSAFE_TO_STD)):
        try:
            return base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_name: str = "personal-agent"
    debug: bool = False
    # 生产默认关闭交互文档（/docs、/redoc、/openapi.json）：本地开发设 true
    enable_docs: bool = False
    # 逗号分隔的 CORS 允许来源；空 = 不启用 CORS（原生 App 无需浏览器跨域）
    cors_allow_origins: str = ""
    # 日志级别（worker 的 basicConfig 与 uvicorn --log-level 用）
    log_level: str = "info"

    # 数据库：生产 postgresql+asyncpg，测试 sqlite+aiosqlite
    database_url: str = "sqlite+aiosqlite:///./app.sqlite3"

    # 鉴权
    jwt_secret: str = DEFAULT_JWT_SECRET
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30

    # LLM：只接受这两个值，拼错启动即报错（而不是静默当 mock 用）
    llm_provider: Literal["mock", "openai"] = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 15.0
    # 仅测试用：生产（postgresql）+ mock 默认拒绝启动（假整理/假问答），
    # 跑 PG 测试套件的 CI/本地环境显式置 true 放行（生产环境不要设置）
    allow_mock_llm_in_prod: bool = False

    # 记忆层：只接受这两个值，未知值（含 mem0——生产后端已拆分至
    # feat/mem0-backend 分支，见 docs/server-invariants.md INV-3/INV-10）
    # 启动即报错（而不是静默退化丢记忆）
    memory_backend: Literal["inmemory", "disabled"] = "inmemory"
    # 仅测试用：生产（postgresql）+ inmemory 默认拒绝启动，跑 PG 测试套件
    # 的 CI/本地环境显式置 true 放行（生产环境不要设置；独立 worker 进程
    # 无论该标志都拒绝，见 app.jobs.worker.check_memory_backend_shareable）
    allow_inmemory_in_tests: bool = False

    # 流水线
    default_timezone: str = "Asia/Shanghai"
    job_max_attempts: int = 3
    # running 状态超过该秒数视为 worker 中断，任务可被回收重跑
    job_lock_timeout_seconds: int = 300

    # 整理 worker
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10

    # /v1/ask 检索条数
    ask_top_k: int = 5

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret_in_production(self) -> "Settings":
        """生产（postgresql）下 JWT_SECRET 必须是 >=32 字节随机值的编码；
        SQLite 开发测试不受影响。"""
        if not self.database_url.startswith("postgresql"):
            return self
        secret = self.jwt_secret
        problems: list[str] = []
        if secret in (DEFAULT_JWT_SECRET, ENV_EXAMPLE_JWT_PLACEHOLDER):
            problems.append("使用了默认/模板占位值")
        lowered = secret.lower()
        if any(word in lowered for word in _INSECURE_SUBSTRINGS):
            problems.append("包含 change-me/secret/password 等占位或人类可读口令字样")
        raw = _decode_secret(secret)
        if raw is None:
            problems.append(
                "不是合法的 hex/base64 编码（hex 需 >=64 位偶数长，"
                "或 base64/base64url 解码 >=32 字节）"
            )
        else:
            if len(raw) < _MIN_SECRET_BYTES:
                problems.append(f"解码后只有 {len(raw)} 字节（< {_MIN_SECRET_BYTES}）")
            if len(set(raw)) < _MIN_SECRET_DISTINCT_BYTES:
                problems.append(
                    f"解码后不同字节取值数不足（{len(set(raw))} < {_MIN_SECRET_DISTINCT_BYTES}）"
                )
        if problems:
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）的 JWT_SECRET 不安全："
                + "；".join(problems)
                + "。请用随机值生成，如 `python -c \"import secrets;"
                  "print(secrets.token_urlsafe(48))\"` 或 `openssl rand -hex 32`"
            )
        return self

    @model_validator(mode="after")
    def _reject_inmemory_memory_backend_in_production(self) -> "Settings":
        """P1：生产（postgresql）下 MEMORY_BACKEND=inmemory 会静默丢记忆
        ——uvicorn 多 worker 与独立 worker 进程各持一份进程内状态，互相
        不可见且重启即失忆。启动即拒绝；仅在单进程跑 PG 测试套件的
        CI/本地环境可用 ALLOW_INMEMORY_IN_TESTS=true 显式放行（独立
        worker 进程不放行，见 app.jobs.worker）。disabled（显式空后端）
        在任何环境都允许；生产记忆后端由后续 PR（feat/mem0-backend）提供。
        """
        if (
            self.database_url.startswith("postgresql")
            and self.memory_backend == "inmemory"
            and not self.allow_inmemory_in_tests
        ):
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）不允许 "
                "MEMORY_BACKEND=inmemory：inmemory 不能跨 api/worker 进程共享，"
                "多进程/重启即丢记忆。生产记忆后端将由后续 PR（feat/mem0-backend）"
                "提供，过渡期可显式设 MEMORY_BACKEND=disabled（关闭记忆功能，"
                "任何环境允许）。仅在单进程跑 PG 测试套件的 CI/本地环境可设 "
                "ALLOW_INMEMORY_IN_TESTS=true 放行（独立 worker 进程无论该标志都拒绝）"
            )
        return self

    @model_validator(mode="after")
    def _require_complete_openai_config(self) -> "Settings":
        """INV-10.1（修 C3，任何环境）：LLM_PROVIDER=openai 时
        LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三者必须齐备——缺任何一项
        启动即 ValueError，而不是运行期拼出相对 /chat/completions URL 或
        空 model，让每次 extract/ask 都失败。"""
        if self.llm_provider != "openai":
            return self
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "LLM_PROVIDER=openai 必须同时设置 " + "、".join(missing) + "（缺任一项启动即失败，不会静默回落）"
            )
        return self

    @model_validator(mode="after")
    def _validate_default_timezone(self) -> "Settings":
        """INV-10.2（修 C7，任何环境）：DEFAULT_TIMEZONE 必须是合法 IANA
        时区名。请求省略 timezone 时该值会落到每条新笔记上，拼错的名字
        会让 naive recorded_at 静默按 UTC 解释、提醒解析回退别的时区。"""
        try:
            ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"DEFAULT_TIMEZONE 必须是合法 IANA 时区名：{self.default_timezone}"
            ) from exc
        return self

    @model_validator(mode="after")
    def _reject_misconfigured_llm_in_production(self) -> "Settings":
        """P2：生产（postgresql）下 LLM 配置错误必须启动报错，而不是静默
        回落 MockLLM（整理/问答全变假实现，问题被掩盖到用户侧）。

        - LLM_PROVIDER=mock：拒绝（假实现），仅跑 PG 测试套件的 CI/本地
          环境可设 ALLOW_MOCK_LLM_IN_PROD=true 放行。
        - openai 缺 key/base_url/model 的拒绝在任何环境生效
          （_require_complete_openai_config）。
        provider 拼错由 Literal 校验直接报错（任何环境）。
        """
        if not self.database_url.startswith("postgresql"):
            return self
        if self.llm_provider == "mock" and not self.allow_mock_llm_in_prod:
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）不允许 LLM_PROVIDER=mock"
                "（整理/问答会是确定性假实现）：请配置 openai。"
                "仅在跑 PG 测试套件的 CI/本地环境可设 ALLOW_MOCK_LLM_IN_PROD=true 放行"
            )
        return self

    @model_validator(mode="after")
    def _require_positive_numeric_settings(self) -> "Settings":
        """INV-10.7（threads8 P2，config.py:99）：数值配置必须为正。0/负值
        会让相应循环或调用静默失效（WORKER_BATCH_SIZE=0 时 worker 日志一切
        正常但循环体永不执行、任何笔记都不被整理）——启动时一次性列出
        **全部**非正字段。新增数值型配置必须加进下表并同步 .env.example
        （docs/server-invariants.md 自查清单第 15 条）。"""
        checks = {
            "ACCESS_TOKEN_TTL_SECONDS": self.access_token_ttl_seconds,
            "REFRESH_TOKEN_TTL_DAYS": self.refresh_token_ttl_days,
            "LLM_TIMEOUT_SECONDS": self.llm_timeout_seconds,
            "JOB_MAX_ATTEMPTS": self.job_max_attempts,
            "JOB_LOCK_TIMEOUT_SECONDS": self.job_lock_timeout_seconds,
            "WORKER_POLL_INTERVAL_SECONDS": self.worker_poll_interval_seconds,
            "WORKER_BATCH_SIZE": self.worker_batch_size,
            "ASK_TOP_K": self.ask_top_k,
        }
        # not (v > 0) 连 NaN 也拦下（NaN <= 0 为 False）
        bad = {name: value for name, value in checks.items() if not (value > 0)}
        if bad:
            raise ValueError(
                "数值配置必须为正（0/负值会让相应循环或调用静默失效）："
                + "、".join(f"{NAME}={value!r}" for NAME, value in bad.items())
            )
        return self

    @model_validator(mode="after")
    def _reject_sqlalchemy_echo(self) -> "Settings":
        # INV-14（threads9）：见 _reject_echo_in_url——DbSettings 同款校验
        _reject_echo_in_url(self.database_url)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _reject_echo_in_url(database_url: str) -> None:
    """INV-14（threads9）：DATABASE_URL 的 URL query 会原样传给
    create_async_engine 当引擎参数——`?echo=true` 静默开 echo，SQL 与
    绑定参数（笔记正文等用户内容）整体进 sqlalchemy.engine 日志。任何
    环境启动即拒；排障需要 SQL 时本地临时改代码，不开配置口。"""
    from sqlalchemy.engine import make_url

    try:
        query = make_url(database_url).query
    except ValueError:
        # URL 本身非法：交给真正创建引擎的路径报错，这里不抢报
        return
    echo = query.get("echo")
    if echo is not None and str(echo).strip().lower() not in ("", "0", "false", "no", "off"):
        raise ValueError(
            "DATABASE_URL 不得通过 URL query 开 SQLAlchemy echo（如 ?echo=true）："
            "echo 会把 SQL 与绑定参数（用户内容）打进 sqlalchemy.engine 日志"
            "（INV-14）。排障需要 SQL 时本地临时改代码。"
        )


class DbSettings(BaseSettings):
    """只含数据库层的配置（threads7 P1 / INV-9 校验分层）。

    init-db（compose 的 one-shot migrate 服务）只做建表：让它加载完整
    Settings 会把 LLM/记忆后端/JWT 的生产校验也拖进建表路径——migrate
    沿用 .env 的本地默认（MEMORY_BACKEND=inmemory、LLM_PROVIDER=mock）
    叠加 compose 的 PG 覆盖时，会撞上生产拓扑校验直接退出，全新部署停在
    建表一步。建表只依赖 DATABASE_URL；api/worker 仍按各自 command 过
    完整 Settings 的启动校验（有测试机械验证，见 tests/test_compose.py）。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./app.sqlite3"

    @model_validator(mode="after")
    def _reject_sqlalchemy_echo(self) -> "DbSettings":
        # 同 Settings（INV-14，threads9）：init-db 的引擎同样吃 URL query
        _reject_echo_in_url(self.database_url)
        return self


@lru_cache
def get_db_settings() -> DbSettings:
    return DbSettings()
