"""mem0 后端适配器。

- mem0ai 的 import 放在构造内：MEMORY_BACKEND=mem0 而未安装 mem0ai 时，
  构造直接抛 ImportError（生产配置错误应当暴露，见 app.memory.base）。
- 向量库 pgvector 与业务库同一个 PostgreSQL 实例；mem0 内部的 LLM/embedding 走
  OpenAI 兼容配置。
- mem0 是同步库，这里用 asyncio.to_thread 包一层。
- add 返回本次写入的全部记忆（metadata 带 note_id/source_rev，INV-3）；
  delete 按 id 列表删（复查不一致时只删本次新增，修 C9）。
"""
from __future__ import annotations

from app.config import get_settings
from app.memory.base import MemoryItem


def _item_from_result(user_id: str, r: dict) -> MemoryItem:
    metadata = r.get("metadata") or {}
    source_rev = metadata.get("source_rev")
    return MemoryItem(
        id=r.get("id") or r.get("memory_id") or "",
        user_id=user_id,
        text=r.get("memory", ""),
        note_id=metadata.get("note_id"),
        source_rev=int(source_rev) if source_rev is not None else None,
    )


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

    async def add(
        self, user_id: str, text: str, *, note_id: str | None = None, source_rev: int | None = None
    ) -> list[MemoryItem]:
        import asyncio

        metadata = {"note_id": note_id, "source_rev": source_rev}
        result = await asyncio.to_thread(
            self._client.add, text, user_id=user_id, metadata=metadata
        )
        # mem0 返回 {"results": [...]}：一次 add 可能产生多条记忆，全部带回
        results = result.get("results", []) if isinstance(result, dict) else []
        return [_item_from_result(user_id, r) for r in results if isinstance(r, dict)]

    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(self._client.search, query, user_id=user_id, limit=top_k)
        raw = results if isinstance(results, list) else results.get("results", [])
        items = [_item_from_result(user_id, r) for r in raw if isinstance(r, dict)]
        for item, r in zip(items, raw):
            item.score = float(r.get("score", 0.0) or 0.0)
        return items

    async def list(self, user_id: str) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(self._client.get_all, user_id=user_id)
        raw = results if isinstance(results, list) else results.get("results", [])
        return [_item_from_result(user_id, r) for r in raw if isinstance(r, dict)]

    async def delete(self, user_id: str, memory_ids: list[str]) -> int:
        import asyncio

        # mem0 的 delete 不区分属主：id 由本服务的 add 返回（同 user 隔离
        # 由调用方保证），逐个删除即可
        for mid in memory_ids:
            if mid:
                await asyncio.to_thread(self._client.delete, mid)
        return len([m for m in memory_ids if m])

    async def delete_by_note(self, user_id: str, note_id: str) -> int:
        # list() 返回 MemoryItem（dataclass），按属性访问而不是 dict
        count = 0
        for item in await self.list(user_id):
            if item.note_id == note_id:
                await self.delete(user_id, [item.id])
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
