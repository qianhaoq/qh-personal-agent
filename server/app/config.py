"""应用配置：全部来自环境变量（.env 亦可）。"""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 明显的占位符，只允许配合 SQLite 开发/测试使用
DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"


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

    # 记忆层
    memory_backend: str = "inmemory"  # inmemory | mem0
    # mem0 的 embedding 单独配置（可以和 LLM 不同源）
    mem0_embedding_base_url: str = ""
    mem0_embedding_model: str = ""

    # 流水线
    default_timezone: str = "Asia/Shanghai"
    job_max_attempts: int = 3

    # 整理 worker
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10

    # /v1/ask 检索条数
    ask_top_k: int = 5

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret_in_production(self) -> "Settings":
        """生产（postgresql）禁止默认/过短 JWT_SECRET；SQLite 开发测试不受影响。"""
        if self.database_url.startswith("postgresql") and (
            self.jwt_secret == DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32
        ):
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）禁止使用默认或过短（<32 字节）"
                f"的 JWT_SECRET（当前长度 {len(self.jwt_secret)}）："
                "请设置 JWT_SECRET 环境变量为至少 32 字节的随机值（如 `openssl rand -hex 32`）"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
