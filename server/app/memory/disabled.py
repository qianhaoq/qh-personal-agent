"""显式关闭记忆功能的空后端（threads7 拆分，INV-10.5）。

生产记忆后端（mem0 + pgvector）已拆分至 feat/mem0-backend 分支（后续 PR）；
在那之前，api/worker 分进程部署没有任何可共享的记忆实现——`disabled` 把
「关闭」做成一个显式、可审计的配置值，而不是：

- null-but-pretend：配个假实现假装在记忆——假 store 仍会「成功写入」，
  观察者无法区分「功能关闭」与「功能在跑」；
- 测试标志放行 inmemory：把生产行为绑在 ALLOW_INMEMORY_IN_TESTS 上，且
  inmemory 在多进程下是真丢数据（不是关闭）；
- 拒绝启动：拆分后到 feat/mem0-backend 落地前，compose 起不来。

语义（INV-5）：memory_sync 照常运行并把 memory_status 标 synced——
「记忆 == f(note)」在空后端下平凡成立（后端里永远没有记忆），不把 disabled
当失败（不标 failed、不无限重试）。/v1/search 返回空 items、/v1/ask 走
「无相关记忆」分支，不报 500。
"""
from __future__ import annotations

from app.memory.base import MemoryItem, MemoryStore


class DisabledMemoryStore(MemoryStore):
    """全部接口的空实现：无任何外部写，任何环境（含生产/worker）允许。"""

    async def add(
        self, user_id: str, text: str, *, note_id: str | None = None, source_rev: int | None = None
    ) -> list[MemoryItem]:
        return []

    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        return []

    async def list(self, user_id: str) -> list[MemoryItem]:
        return []

    async def delete(self, user_id: str, memory_ids: list[str]) -> int:
        return 0

    async def delete_by_note(self, user_id: str, note_id: str) -> int:
        return 0

    async def delete_all(self, user_id: str) -> int:
        return 0
