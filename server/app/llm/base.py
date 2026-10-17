"""LLM 客户端接口。"""
from typing import Any, Protocol

# 结构化抽取的 JSON Schema（见 tech-arch 3.1，S1 简化为所需字段）
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "笔记标题，不超过 20 字"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "1-5 个标签"},
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "due": {"type": "string", "description": "ISO 8601 时间，带时区偏移"},
                },
                "required": ["text"],
            },
        },
        "suggested_reminders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "fire_at": {"type": "string", "description": "ISO 8601 时间，带时区偏移"},
                },
                "required": ["title", "fire_at"],
            },
        },
    },
    "required": ["title", "tags", "todos", "suggested_reminders"],
}


class LLMClient(Protocol):
    async def chat(self, messages: list[dict], response_format: dict | None = None) -> str:
        """返回模型文本输出。调用方自行解析。"""
        ...


async def get_llm_client() -> LLMClient:
    """按配置构造 LLM 客户端。

    不再静默回落 MockLLM（P2）：LLM_PROVIDER=openai 而缺 LLM_API_KEY、
    或生产（postgresql）配了 mock，都直接抛错——配置错误必须暴露出来，
    假实现混进生产会掩盖问题到用户侧。启动侧的同名校验见
    app.config._reject_misconfigured_llm_in_production，这里是运行时兜底。
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "openai":
        if not settings.llm_api_key:
            raise RuntimeError("LLM_PROVIDER=openai 但未设置 LLM_API_KEY（不会静默回落 MockLLM）")
        from app.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    # mock：生产下 config 已在启动时拒绝；这里再挡一层绕过启动校验的场景
    if settings.database_url.startswith("postgresql") and not settings.allow_mock_llm_in_prod:
        raise RuntimeError(
            "生产环境不允许 LLM_PROVIDER=mock（仅测试可设 ALLOW_MOCK_LLM_IN_PROD=true）"
        )
    from app.llm.mock import MockLLM

    return MockLLM()
