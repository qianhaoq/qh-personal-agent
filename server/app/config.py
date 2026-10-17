"""应用配置：全部来自环境变量（.env 亦可）。"""
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 明显的占位符，只允许配合 SQLite 开发/测试使用
DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"
# .env.example 里的占位值（测试会读取 .env.example 校验与此常量一致）
ENV_EXAMPLE_JWT_PLACEHOLDER = "change-me-to-a-long-random-secret"
# 明显的占位/示例字样，出现在生产密钥里即拒绝（大小写不敏感）
_INSECURE_SUBSTRINGS = ("change-me", "changeme", "example", "placeholder")
# 生产密钥的最低强度：长度与不同字符数
_MIN_SECRET_LENGTH = 32
_MIN_SECRET_DISTINCT_CHARS = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_name: str = "personal-agent"
    debug: bool = False

    # 数据库：生产 postgresql+asyncpg，测试 sqlite+aiosqlite
    database_url: str = "sqlite+aiosqlite:///./app.sqlite3"

    # 鉴权
    jwt_secret: str = DEFAULT_JWT_SECRET
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30

    # LLM
    llm_provider: str = "mock"  # mock | openai
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 15.0

    # 记忆层：只接受这两个值，未知值启动即报错（而不是静默退化丢记忆）
    memory_backend: Literal["inmemory", "mem0"] = "inmemory"
    # mem0 的 embedding 单独配置（可以和 LLM 不同源）
    mem0_embedding_base_url: str = ""
    mem0_embedding_model: str = ""

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
        """生产（postgresql）下 JWT_SECRET 必须是强随机值；SQLite 开发测试不受影响。"""
        if not self.database_url.startswith("postgresql"):
            return self
        secret = self.jwt_secret
        problems: list[str] = []
        if secret in (DEFAULT_JWT_SECRET, ENV_EXAMPLE_JWT_PLACEHOLDER):
            problems.append("使用了默认/模板占位值")
        lowered = secret.lower()
        if any(word in lowered for word in _INSECURE_SUBSTRINGS):
            problems.append("包含 change-me/changeme/example/placeholder 等占位字样")
        if len(secret) < _MIN_SECRET_LENGTH:
            problems.append(f"长度不足（{len(secret)} < {_MIN_SECRET_LENGTH}）")
        if len(set(secret)) < _MIN_SECRET_DISTINCT_CHARS:
            problems.append(f"不同字符数不足（{len(set(secret))} < {_MIN_SECRET_DISTINCT_CHARS}）")
        if problems:
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）的 JWT_SECRET 不安全："
                + "；".join(problems)
                + "。请设置 JWT_SECRET 为随机值（如 `python -c \"import secrets;"
                  "print(secrets.token_urlsafe(48))\"`）"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
