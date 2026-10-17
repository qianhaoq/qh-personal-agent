"""mem0 后端适配器（钉住 mem0ai==2.2.1，见 requirements-mem0.txt）。

- mem0ai 的 import 放在构造内：MEMORY_BACKEND=mem0 而未安装 mem0ai 时，
  构造直接抛 ImportError（生产配置错误应当暴露，见 app.memory.base）。
- 向量库 pgvector 与业务库同一个 PostgreSQL 实例；mem0 内部的 LLM/embedding 走
  OpenAI 兼容配置。
- mem0 是同步库，这里用 asyncio.to_thread 包一层。
- add 返回本次写入的全部记忆（metadata 带 note_id/source_rev，INV-3）；
  delete 按 id 列表删（复查不一致时只删本次新增，修 C9）。

mem0 2.x API 适配（2.2.1 实测，钉版本防漂移）：
- search/get_all **拒绝**顶层 user_id/agent_id/run_id（ValueError），作用域
  一律走 `filters={"user_id": ...}`；search 的条数参数是 `top_k`（无 limit）。
- get_all 默认 top_k=20 会截断：delete_by_note 需要全量，显式放大到
  _LIST_TOP_K。
- add 的返回有两种形状：infer=True 主路径返回**裸 list**，
  infer=False 原始路径返回 {"results": [...]}——两种都归一。
- delete 对不存在的 id 抛 ValueError：按「已删除」处理（幂等），不中断
  批量清理。
"""
from __future__ import annotations

from app.config import get_settings
from app.memory.base import MemoryItem

# list() 用于 delete_by_note/delete_all 的全量枚举：必须盖住单用户全部
# 记忆，否则 mem0 get_all 的默认 top_k=20 会静默截断、留下删不净的记忆
_LIST_TOP_K = 1000


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


def _results_of(result: object) -> list[dict]:
    """归一 mem0 各版本的返回形状：dict 取 results；2.x 的 infer 主路径
    直接返回裸 list。"""
    if isinstance(result, dict):
        raw = result.get("results", [])
    elif isinstance(result, list):
        raw = result
    else:
        raw = []
    return [r for r in raw if isinstance(r, dict)]


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
        # add 接受顶层 user_id（2.x 只对 search/get_all 收回了顶层实体参数）
        result = await asyncio.to_thread(
            self._client.add, text, user_id=user_id, metadata=metadata
        )
        # 一次 add 可能产生多条记忆，全部带回（形状归一见模块 docstring）。
        # 2.2.1 的 infer 主路径不回显 metadata：note_id/source_rev 用自己的
        # 入参补齐（INV-3 归属信息不能依赖后端回显）
        items = [_item_from_result(user_id, r) for r in _results_of(result)]
        for item in items:
            if item.note_id is None:
                item.note_id = note_id
            if item.source_rev is None:
                item.source_rev = source_rev
        return items

    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(
            self._client.search, query, filters={"user_id": user_id}, top_k=top_k
        )
        raw = _results_of(results)
        items = [_item_from_result(user_id, r) for r in raw]
        for item, r in zip(items, raw):
            item.score = float(r.get("score", 0.0) or 0.0)
        return items

    async def list(self, user_id: str) -> list[MemoryItem]:
        import asyncio

        results = await asyncio.to_thread(
            self._client.get_all, filters={"user_id": user_id}, top_k=_LIST_TOP_K
        )
        return [_item_from_result(user_id, r) for r in _results_of(results)]

    async def delete(self, user_id: str, memory_ids: list[str]) -> int:
        import asyncio

        # mem0 的 delete 不区分属主：id 由本服务的 add 返回（同 user 隔离
        # 由调用方保证），逐个删除即可；2.x 对不存在的 id 抛 ValueError，
        # 按「已删除」吞掉（幂等），不中断批量清理
        count = 0
        for mid in memory_ids:
            if not mid:
                continue
            try:
                await asyncio.to_thread(self._client.delete, mid)
            except ValueError:
                pass
            count += 1
        return count

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
