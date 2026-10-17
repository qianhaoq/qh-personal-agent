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
    from app.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "openai" and settings.llm_api_key:
        from app.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    from app.llm.mock import MockLLM

    return MockLLM()
