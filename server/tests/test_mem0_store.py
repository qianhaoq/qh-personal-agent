"""mem0 后端适配器：用假的 mem0 client 测试（本机不装 mem0ai）。

fake 严格按钉住的 mem0ai==2.2.1 签名建模（requirements-mem0.txt）：
- get_all/search 拒绝顶层 user_id/agent_id/run_id（传了就 ValueError），
  作用域只认 filters={"user_id": ...}；search 的条数参数是 top_k；
- get_all 按 top_k 截断（默认 20）；
- add 的 infer 主路径返回裸 list（不是 {"results": [...]}）；
- delete 对不存在的 id 抛 ValueError。
适配层若退回旧调用方式，这些测试会立刻红（4c1c9be P2-3）。
"""
import sys
import types

import pytest

_ENTITY_PARAMS = {"user_id", "agent_id", "run_id"}


def _reject_top_level_entity(kwargs: dict, method: str) -> None:
    """复刻 mem0 2.2.1 的 _reject_top_level_entity_params。"""
    bad = _ENTITY_PARAMS & set(kwargs)
    if bad:
        raise ValueError(
            f"Top-level entity parameters {bad} are not supported in {method}(). "
            f"Use filters={{'user_id': '...'}} instead."
        )


class _FakeMem0Memory:
    """duck-typed mem0.Memory（2.2.1 签名）：add/get_all/search/delete 全记调用。"""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {
            "m1": {"id": "m1", "memory": "周报要点", "metadata": {"note_id": "nte_a"}},
            "m2": {"id": "m2", "memory": "买菜清单", "metadata": {"note_id": "nte_b"}},
            "m3": {"id": "m3", "memory": "会议纪要", "metadata": {"note_id": "nte_a"}},
            "m4": {"id": "m4", "memory": "无 note_id 的记忆", "metadata": {}},
        }
        self.deleted: list[str] = []  # 尝试过的 delete（含随后抛 ValueError 的）
        self.add_calls: list[tuple[str, dict]] = []  # (text, kwargs)
        self.search_calls: list[tuple[str, dict]] = []  # (query, kwargs)
        self.get_all_calls: list[dict] = []  # kwargs
        self.delete_all_calls: list[dict] = []
        self._seq = 0

    @classmethod
    def from_config(cls, config: dict) -> "_FakeMem0Memory":
        return cls()

    # ---- add：顶层 user_id 仍合法；infer 主路径返回裸 list ----

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None, metadata=None, **kwargs):
        self.add_calls.append((messages, dict(kwargs, user_id=user_id, metadata=metadata)))
        self._seq += 1
        results = []
        # 一次 add 产出两条：验证“返回全部写入”而不是只回第一条；
        # 2.2.1 的 infer 路径不回显 metadata（只有 id/memory/event）
        for suffix in ("", "_续"):
            mid = f"m_add{self._seq}{suffix}"
            self.items[mid] = {"id": mid, "memory": messages + suffix}
            results.append({"id": mid, "memory": messages + suffix, "event": "ADD"})
        return results

    # ---- get_all：只认 filters；top_k 截断（默认 20）----

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        _reject_top_level_entity(kwargs, "get_all")
        effective = dict(filters or {})
        if not any(key in effective for key in _ENTITY_PARAMS):
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id")
        self.get_all_calls.append(dict(kwargs, filters=dict(effective), top_k=top_k))
        return {"results": list(self.items.values())[:top_k]}

    # ---- search：只认 filters + top_k（没有 limit 参数）----

    def search(self, query, *, top_k=20, filters=None, threshold=0.1, **kwargs):
        _reject_top_level_entity(kwargs, "search")
        effective = dict(filters or {})
        if not any(key in effective for key in _ENTITY_PARAMS):
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id")
        self.search_calls.append((query, dict(kwargs, top_k=top_k, filters=dict(effective))))
        hits = [
            dict(item, score=0.9) for item in self.items.values() if item["memory"] in query
        ]
        return {"results": hits[:top_k]}

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        if memory_id not in self.items:
            # mem0 2.2.1：不存在的 id 抛 ValueError
            raise ValueError(f"Memory with id {memory_id} not found")
        self.items.pop(memory_id, None)

    def delete_all(self, user_id=None, agent_id=None, run_id=None):
        self.delete_all_calls.append({"user_id": user_id})
        if not (user_id or agent_id or run_id):
            raise ValueError("At least one filter is required to delete all memories")
        self.items.clear()
        return {"message": "All memories deleted"}


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
    条目转成 MemoryItem（一次 add 可能产生多条；2.2.1 infer 路径返回裸
    list 且不回显 metadata——note_id/rev 由适配层用自己的入参补齐）。"""
    store = _make_store(fake_mem0)
    items = await store.add("usr_1", "对花生过敏", note_id="nte_x", source_rev=3)
    assert len(items) == 2
    assert all(m.note_id == "nte_x" and m.source_rev == 3 for m in items)
    assert all(m.user_id == "usr_1" and m.text.startswith("对花生过敏") for m in items)
    assert len({m.id for m in items}) == 2

    # 客户端调用形状：顶层 user_id（2.x 只收回 search/get_all 的）+ metadata
    text, kwargs = store._client.add_calls[0]
    assert text == "对花生过敏"
    assert kwargs["user_id"] == "usr_1"
    assert kwargs["metadata"] == {"note_id": "nte_x", "source_rev": 3}


async def test_search_scopes_user_via_filters_and_top_k(fake_mem0):
    """2.2.1：search 作用域走 filters={"user_id": ...}、条数走 top_k——
    旧式 search(q, user_id=..., limit=...) 会被 mem0 拒绝（fake 同样抛）。"""
    store = _make_store(fake_mem0)
    items = await store.search("usr_1", "周报要点", top_k=3)
    query, kwargs = store._client.search_calls[0]
    assert (query, kwargs["filters"], kwargs["top_k"]) == ("周报要点", {"user_id": "usr_1"}, 3)
    assert "user_id" not in kwargs and "limit" not in kwargs
    assert [m.id for m in items] == ["m1"]
    assert items[0].score == 0.9
    assert items[0].text == "周报要点"
    assert items[0].note_id == "nte_a"


async def test_get_all_scopes_user_via_filters_and_avoids_truncation(fake_mem0):
    """2.2.1：get_all 走 filters 且默认 top_k=20 会截断——list()（服务于
    delete_by_note/delete_all 的全量枚举）必须显式放大 top_k。"""
    store = _make_store(fake_mem0)
    # 撑过默认 20 条的截断线
    for i in range(25):
        store._client.items[f"x{i}"] = {"id": f"x{i}", "memory": f"记忆{i}", "metadata": {}}
    items = await store.list("usr_1")
    assert len(items) == 29, "list 必须返回全量（默认 top_k=20 会截断）"
    kwargs = store._client.get_all_calls[0]
    assert kwargs["filters"] == {"user_id": "usr_1"}
    assert kwargs["top_k"] >= 100, "delete_by_note 依赖全量列举，top_k 必须放大"


async def test_delete_by_ids_delegates_each_id_and_tolerates_missing(fake_mem0):
    """delete(ids)（修 C9 的复查清理路径）：逐 id 调 client.delete，返回
    非空 id 的个数；2.2.1 对不存在的 id 抛 ValueError——按已删除吞掉，
    不中断批量清理。"""
    store = _make_store(fake_mem0)
    count = await store.delete("usr_1", ["m1", "m2", "", "m_nothing"])
    assert count == 3  # 空串跳过；不存在的 id 也计入返回数（已删除=成功）
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


async def test_delete_all_delegates_with_top_level_user_id(fake_mem0):
    """delete_all 在 2.2.1 仍收顶层 user_id（未被 filters 化），返回删除前
    的条数。"""
    store = _make_store(fake_mem0)
    count = await store.delete_all("usr_1")
    assert count == 4
    assert store._client.delete_all_calls == [{"user_id": "usr_1"}]
    assert await store.list("usr_1") == []


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
