"""应用配置：全部来自环境变量（.env 亦可）。"""
import base64
import binascii
import string
from functools import lru_cache
from typing import Literal

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
