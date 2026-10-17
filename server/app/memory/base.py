"""MemoryStore 抽象：全部按 user_id 隔离。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class MemoryItem:
    id: str
    user_id: str
    text: str
    note_id: str | None = None
    score: float = 0.0
    created_at: str | None = None


class MemoryStore(ABC):
    @abstractmethod
    async def add(self, user_id: str, text: str, note_id: str | None = None) -> MemoryItem: ...

    @abstractmethod
    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]: ...

    @abstractmethod
    async def list(self, user_id: str) -> list[MemoryItem]: ...

    @abstractmethod
    async def delete(self, user_id: str, memory_id: str) -> bool: ...

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
            try:
                from app.memory.mem0_store import Mem0Store

                _store = Mem0Store()
            except ImportError:
                # mem0ai 未安装：退化为内存实现，保证其他功能可用
                from app.memory.inmemory import InMemoryStore

                _store = InMemoryStore()
        else:
            from app.memory.inmemory import InMemoryStore

            _store = InMemoryStore()
    return _store


def set_memory_store(store: MemoryStore) -> None:
    """测试/替换后端用。"""
    global _store
    _store = store
