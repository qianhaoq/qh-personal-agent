"""mem0 后端适配器。

- mem0ai 的 import 放在构造内：MEMORY_BACKEND=mem0 而未安装 mem0ai 时，
  构造直接抛 ImportError（生产配置错误应当暴露，见 app.memory.base）。
- 向量库 pgvector 与业务库同一个 PostgreSQL 实例；mem0 内部的 LLM/embedding 走
  OpenAI 兼容配置。
- mem0 是同步库，这里用 asyncio.to_thread 包一层。
"""
from app.config import get_settings
from app.memory.base import MemoryItem


class Mem0Store:
    """实现 app.memory.base.MemoryStore 的接口（鸭子类型，避免继承时触发顶层 import）。"""

    def __init__(self) -> None:
        from mem0 import Memory  # mem0 未安装时在这里才报 ImportError

        settings = get_settings()
        config = {
            "vector_store": {
                "provider": "pgvector",
                "config": {
                    "connection_string": _pg_connection_string(settings.database_url),
                    "collection_name": "personal_agent_memories",
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "base_url": settings.llm_base_url,
                    "api_key": settings.llm_api_key,
                    "model": settings.llm_model,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "base_url": settings.mem0_embedding_base_url or settings.llm_base_url,
                    "api_key": settings.llm_api_key,
                    "model": settings.mem0_embedding_model or "text-embedding-3-small",
                },
            },
        }
        self._client = Memory.from_config(config)

    async def add(self, user_id: str, text: str, note_id: str | None = None) -> MemoryItem:
        import asyncio

        metadata = {"note_id": note_id} if note_id else {}
        result = await asyncio.to_thread(
            self._client.add, text, user_id=user_id, metadata=metadata
        )
        # mem0 返回 {"results": [...]}；取第一个结果包装成统一形状
        results = result.get("results", []) if isinstance(result, dict) else []
        first = results[0] if results else {}
        memory_id = first.get("id") or first.get("memory_id") or ""
        return MemoryItem(
            id=memory_id, user_id=user_id, text=first.get("memory", text), note_id=note_id
        )

    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(self._client.search, query, user_id=user_id, limit=top_k)
        raw = results if isinstance(results, list) else results.get("results", [])
        return [
            MemoryItem(
                id=r.get("id") or r.get("memory_id"),
                user_id=user_id,
                text=r.get("memory", ""),
                note_id=(r.get("metadata") or {}).get("note_id"),
                score=r.get("score", 0.0),
            )
            for r in raw
        ]

    async def list(self, user_id: str) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(self._client.get_all, user_id=user_id)
        raw = results if isinstance(results, list) else results.get("results", [])
        return [
            MemoryItem(
                id=r.get("id") or r.get("memory_id"),
                user_id=user_id,
                text=r.get("memory", ""),
                note_id=(r.get("metadata") or {}).get("note_id"),
            )
            for r in raw
        ]

    async def delete(self, user_id: str, memory_id: str) -> bool:
        import asyncio

        await asyncio.to_thread(self._client.delete, memory_id)
        return True

    async def delete_by_note(self, user_id: str, note_id: str) -> int:
        # list() 返回 MemoryItem（dataclass），按属性访问而不是 dict
        count = 0
        for item in await self.list(user_id):
            if item.note_id == note_id:
                await self.delete(user_id, item.id)
                count += 1
        return count

    async def delete_all(self, user_id: str) -> int:
        import asyncio

        items = await self.list(user_id)
        await asyncio.to_thread(self._client.delete_all, user_id=user_id)
        return len(items)


def _pg_connection_string(async_url: str) -> str:
    """把 SQLAlchemy async URL 换成 mem0 要的同步驱动串。"""
    for prefix, repl in (
        ("postgresql+asyncpg://", "postgresql://"),
        ("sqlite+aiosqlite://", "sqlite://"),
    ):
        if async_url.startswith(prefix):
            return repl + async_url[len(prefix) :]
    return async_url
