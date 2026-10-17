"""mem0 后端适配器：用假的 mem0 client 测试（本机不装 mem0ai）。"""
import sys
import types

import pytest


class _FakeMem0Memory:
    """duck-typed mem0.Memory：add/get_all/delete 走 mem0 的 dict 返回形状。"""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {
            "m1": {"id": "m1", "memory": "周报要点", "metadata": {"note_id": "nte_a"}},
            "m2": {"id": "m2", "memory": "买菜清单", "metadata": {"note_id": "nte_b"}},
            "m3": {"id": "m3", "memory": "会议纪要", "metadata": {"note_id": "nte_a"}},
            "m4": {"id": "m4", "memory": "无 note_id 的记忆", "metadata": {}},
        }
        self.deleted: list[str] = []
        self.add_calls: list[tuple[str, str, dict]] = []
        self._seq = 0

    @classmethod
    def from_config(cls, config: dict) -> "_FakeMem0Memory":
        return cls()

    def add(self, text: str, user_id: str = "", metadata: dict | None = None) -> dict:
        self.add_calls.append((text, user_id, dict(metadata or {})))
        self._seq += 1
        results = []
        # 一次 add 产出两条：验证“返回全部写入”而不是只回第一条
        for suffix in ("", "_续"):
            mid = f"m_add{self._seq}{suffix}"
            entry = {"id": mid, "memory": text + suffix, "metadata": dict(metadata or {})}
            self.items[mid] = entry
            results.append(entry)
        return {"results": results}

    def get_all(self, user_id: str) -> dict:
        return {"results": list(self.items.values())}

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)
        self.items.pop(memory_id, None)


@pytest.fixture
def fake_mem0(monkeypatch):
    module = types.ModuleType("mem0")
    module.Memory = _FakeMem0Memory
    monkeypatch.setitem(sys.modules, "mem0", module)
    return module


def _make_store(fake_mem0):
    from app.memory.mem0_store import Mem0Store

    return Mem0Store()


async def test_add_returns_all_written_items_with_note_id_and_rev(fake_mem0):
    """INV-3：add 的 metadata 必带 note_id/source_rev，且把 mem0 返回的全部
    条目转成 MemoryItem（一次 add 可能产生多条）。"""
    store = _make_store(fake_mem0)
    items = await store.add("usr_1", "对花生过敏", note_id="nte_x", source_rev=3)
    assert len(items) == 2
    assert all(m.note_id == "nte_x" and m.source_rev == 3 for m in items)
    assert all(m.user_id == "usr_1" and m.text.startswith("对花生过敏") for m in items)
    assert len({m.id for m in items}) == 2

    text, user_id, metadata = store._client.add_calls[0]
    assert (text, user_id) == ("对花生过敏", "usr_1")
    assert metadata == {"note_id": "nte_x", "source_rev": 3}


async def test_delete_by_ids_delegates_each_id_and_returns_count(fake_mem0):
    """delete(ids)（修 C9 的复查清理路径）：逐 id 调 client.delete，返回
    非空 id 的个数。"""
    store = _make_store(fake_mem0)
    count = await store.delete("usr_1", ["m1", "m2", "", "m_nothing"])
    assert count == 3  # 空串跳过；不存在的 id 也计入返回数（mem0 形状如此）
    assert store._client.deleted == ["m1", "m2", "m_nothing"]
    remaining = {m.id for m in await store.list("usr_1")}
    assert remaining == {"m3", "m4"}


async def test_delete_by_note_removes_only_matching_memories(fake_mem0):
    store = _make_store(fake_mem0)
    count = await store.delete_by_note("usr_1", "nte_a")
    assert count == 2
    assert set(store._client.deleted) == {"m1", "m3"}
    remaining = await store.list("usr_1")
    assert {m.id for m in remaining} == {"m2", "m4"}


async def test_delete_by_note_no_match_returns_zero(fake_mem0):
    store = _make_store(fake_mem0)
    assert await store.delete_by_note("usr_1", "nte_nope") == 0
    assert store._client.deleted == []


async def test_list_maps_mem0_dict_to_memory_item(fake_mem0):
    store = _make_store(fake_mem0)
    items = {m.id: m for m in await store.list("usr_1")}
    assert items["m1"].note_id == "nte_a"
    assert items["m1"].text == "周报要点"
    assert items["m4"].note_id is None


async def test_mem0_backend_without_mem0ai_raises_instead_of_fallback(monkeypatch):
    """MEMORY_BACKEND=mem0 而未装 mem0ai：抛 ImportError，不静默退回 inmemory。"""
    import importlib.util

    if importlib.util.find_spec("mem0") is not None:
        pytest.skip("本环境已安装 mem0ai，无法测试未安装路径")
    from app.config import get_settings
    from app.memory import base

    monkeypatch.setattr(base, "_store", None)
    monkeypatch.setattr(get_settings(), "memory_backend", "mem0")
    with pytest.raises(ImportError):
        base.get_memory_store()
