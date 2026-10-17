"""MemoryStore 抽象：全部按 user_id 隔离。

记忆归属（INV-3，docs/server-invariants.md）：每条由本服务写入的记忆
metadata 必含 note_id 与 source_rev——记忆只属于 note 的当前 rev。
写删记忆的唯一入口是 memory_sync job（app/pipeline/memory_sync.py）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class MemoryItem:
    id: str
    user_id: str
    text: str
    note_id: str | None = None
    source_rev: int | None = None
    score: float = 0.0
    created_at: str | None = None


class MemoryStore(ABC):
    """add 返回本次写入的全部记忆（mem0 一次 add 可能产生多条）；
    delete 按 memory id 列表精确删（复查不一致时只删本次新增，修 C9）。"""

    @abstractmethod
    async def add(
        self, user_id: str, text: str, *, note_id: str | None = None, source_rev: int | None = None
    ) -> list[MemoryItem]: ...

    @abstractmethod
    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]: ...

    @abstractmethod
    async def list(self, user_id: str) -> list[MemoryItem]: ...

    @abstractmethod
    async def delete(self, user_id: str, memory_ids: list[str]) -> int: ...

    @abstractmethod
    async def delete_by_note(self, user_id: str, note_id: str) -> int: ...

    @abstractmethod
    async def delete_all(self, user_id: str) -> int: ...


_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        from app.config import get_settings

        backend = get_settings().memory_backend
        if backend == "mem0":
            # mem0ai 未安装时这里直接抛 ImportError：配置成 mem0 的环境静默退化
            # 成 inmemory 会让记忆悄悄丢失，错误必须暴露出来
            from app.memory.mem0_store import Mem0Store

            _store = Mem0Store()
        elif backend == "inmemory":
            from app.memory.inmemory import InMemoryStore

            _store = InMemoryStore()
        else:
            # config 已用 Literal 限制取值，正常到不了这里；防止未来新增后端
            # 或绕过校验直接改配置时静默退化成错误实现
            raise ValueError(f"未知 MEMORY_BACKEND：{backend}（只支持 inmemory | mem0）")
    return _store


def set_memory_store(store: MemoryStore) -> None:
    """测试/替换后端用。"""
    global _store
    _store = store
