"""进程内记忆实现：字符级/关键词混合相似度，测试与本地开发用。

枚举语义与 Mem0Store 对齐（threads5 P1-1，INV-3）：list() 可注入页上限
（list_page_size），用户记忆数达到上限即抛 MemoryEnumerationIncomplete——
进程内字典本身不会截断，上限只是让「取不全即报错」的行为在两个后端
都可测。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from app.common import new_id
from app.memory.base import MemoryEnumerationIncomplete, MemoryItem, MemoryStore


def similarity(a: str, b: str) -> float:
    """字符级 SequenceMatcher + 关键词重合，取较大者。"""
    if not a or not b:
        return 0.0
    char_score = SequenceMatcher(None, a, b).ratio()
    aw, bw = set(_tokenize(a)), set(_tokenize(b))
    kw_score = len(aw & bw) / len(aw | bw) if aw | bw else 0.0
    return max(char_score, kw_score)


def _tokenize(text: str) -> set[str]:
    """简易中文分词：滑窗 2-gram + 英文/数字词。"""
    tokens = set()
    import re

    for w in re.findall(r"[A-Za-z0-9]+", text):
        if len(w) >= 2:
            tokens.add(w.lower())
    cleaned = re.sub(r"[^一-鿿]", "", text)
    for i in range(len(cleaned) - 1):
        tokens.add(cleaned[i : i + 2])
    return tokens


class InMemoryStore(MemoryStore):
    def __init__(self, *, list_page_size: int | None = None) -> None:
        self._items: dict[str, MemoryItem] = {}  # memory_id -> item
        # list() 的页上限（None = 不限）：注入小值即可模拟「一页取满且
        # 无法推进」的枚举不完整（INV-3）
        self.list_page_size = list_page_size

    async def add(
        self, user_id: str, text: str, *, note_id: str | None = None, source_rev: int | None = None
    ) -> list[MemoryItem]:
        item = MemoryItem(id=new_id("mem"), user_id=user_id, text=text, note_id=note_id, source_rev=source_rev)
        self._items[item.id] = item
        return [item]

    async def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        scored = [
            (similarity(query, item.text), item)
            for item in self._items.values()
            if item.user_id == user_id
        ]
        scored = [(s, it) for s, it in scored if s > 0.05]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, item in scored[:top_k]:
            results.append(
                MemoryItem(
                    id=item.id,
                    user_id=item.user_id,
                    text=item.text,
                    note_id=item.note_id,
                    source_rev=item.source_rev,
                    score=round(score, 4),
                    created_at=item.created_at,
                )
            )
        return results

    async def list(self, user_id: str) -> list[MemoryItem]:
        items = [item for item in self._items.values() if item.user_id == user_id]
        if self.list_page_size is not None and len(items) >= self.list_page_size:
            raise MemoryEnumerationIncomplete(
                f"inmemory list 取满一页（{len(items)}/{self.list_page_size}），"
                "按不完整处理（INV-3）"
            )
        return items

    async def delete(self, user_id: str, memory_ids: list[str]) -> int:
        """按 id 精确删（只删属于该用户的）：复查不一致时清理本次新增用。"""
        deleted = 0
        for mid in memory_ids:
            item = self._items.get(mid)
            if item is not None and item.user_id == user_id:
                del self._items[mid]
                deleted += 1
        return deleted

    async def delete_by_note(self, user_id: str, note_id: str) -> int:
        ids = [
            item.id
            for item in self._items.values()
            if item.user_id == user_id and item.note_id == note_id
        ]
        for mid in ids:
            del self._items[mid]
        return len(ids)

    async def delete_all(self, user_id: str) -> int:
        ids = [item.id for item in self._items.values() if item.user_id == user_id]
        for mid in ids:
            del self._items[mid]
        return len(ids)
