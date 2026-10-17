"""mem0 后端适配器：用假的 mem0 client 测试（本机不装 mem0ai）。

fake 严格按钉住的 mem0ai==2.2.1 签名建模（requirements-mem0.txt）：
- get_all/search 拒绝顶层 user_id/agent_id/run_id（传了就 ValueError），
  作用域只认 filters={"user_id": ...}；search 的条数参数是 top_k；
- get_all 按 top_k 截断（默认 20）；filters 的 **metadata 键**像 pgvector
  一样下推过滤（threads5 P1-1：delete_by_note 靠它精确枚举）；
- add 的 infer 主路径返回裸 list（不是 {"results": [...]}）；
- delete 对不存在的 id 抛 ValueError。
适配层若退回旧调用方式，这些测试会立刻红（4c1c9be P2-3）。
"""
import sys
import types

import pytest

from app.memory.base import MemoryEnumerationIncomplete

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

    # ---- get_all：只认 filters；top_k 截断（默认 20）；metadata 键下推过滤 ----

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        _reject_top_level_entity(kwargs, "get_all")
        effective = dict(filters or {})
        if not any(key in effective for key in _ENTITY_PARAMS):
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id")
        self.get_all_calls.append(dict(kwargs, filters=dict(effective), top_k=top_k))
        # pgvector 语义：非实体键按 payload metadata 精确匹配（真实后端是
        # payload->>'note_id' = ...）。实体键（user_id 等）在真实后端也过滤
        # payload 顶层字段；fake 的条目本就同属一个用户，跳过即可
        matched = [
            item
            for item in self.items.values()
            if all(
                item.get("metadata", {}).get(k) == v
                for k, v in effective.items()
                if k not in _ENTITY_PARAMS
            )
        ]
        return {"results": matched[:top_k]}

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


def _make_store(fake_mem0, **kwargs):
    from app.memory.mem0_store import Mem0Store

    return Mem0Store(**kwargs)


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
    全量枚举）必须显式放大 top_k。"""
    store = _make_store(fake_mem0)
    # 撑过 mem0 默认 20 条的截断线
    for i in range(25):
        store._client.items[f"x{i}"] = {"id": f"x{i}", "memory": f"记忆{i}", "metadata": {}}
    items = await store.list("usr_1")
    assert len(items) == 29, "list 必须返回全量（mem0 默认 top_k=20 会截断）"
    kwargs = store._client.get_all_calls[0]
    assert kwargs["filters"] == {"user_id": "usr_1"}
    assert kwargs["top_k"] >= 100, "全量列举的页大小必须显式放大（默认 20 截断）"


async def test_list_raises_when_page_full_cannot_advance(fake_mem0):
    """threads5 P1-1 / INV-3：get_all 无游标分页——一页取满即无法证明
    已取全，list() 抛 MemoryEnumerationIncomplete（调用方让 job 失败
    重试），绝不静默截断。"""
    store = _make_store(fake_mem0, list_page_size=2)
    with pytest.raises(MemoryEnumerationIncomplete):
        await store.list("usr_1")  # 基线 fake 已有 4 条 > 页上限 2

    # 取不满一页 = 可证明取全，照常返回
    store2 = _make_store(fake_mem0, list_page_size=5)
    assert len(await store2.list("usr_1")) == 4


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


async def test_delete_all_exhaustive_loop_returns_exact_count(fake_mem0):
    """delete_all 走与 delete_by_note 同一套「边取边删」穷尽循环（threads5
    P1-1：client.delete_all 不可知确切条数且无页判定）——返回删除前条数，
    不再委托 client.delete_all。"""
    store = _make_store(fake_mem0)
    count = await store.delete_all("usr_1")
    assert count == 4
    assert store._client.delete_all_calls == [], "不应再走 client.delete_all"
    assert set(store._client.deleted) == {"m1", "m2", "m3", "m4"}
    assert await store.list("usr_1") == []


async def test_delete_by_note_filters_by_note_metadata_and_paginates(fake_mem0):
    """threads5 P1-1 / INV-3：delete_by_note 按 (user_id, note_id) 在后端
    过滤枚举（filters 带 metadata 键，pgvector 语义），页大小注入 2 验证
    跨页删净——3 条匹配跨两轮删完，别家的 note 一条不动。"""
    store = _make_store(fake_mem0, list_page_size=2)
    client = store._client
    client.items = {  # dict 保序：a1,a2,a3 是 nte_p 的三条
        "a1": {"id": "a1", "memory": "甲", "metadata": {"note_id": "nte_p"}},
        "b1": {"id": "b1", "memory": "乙", "metadata": {"note_id": "nte_other"}},
        "a2": {"id": "a2", "memory": "丙", "metadata": {"note_id": "nte_p"}},
        "b2": {"id": "b2", "memory": "丁", "metadata": {}},
        "a3": {"id": "a3", "memory": "戊", "metadata": {"note_id": "nte_p"}},
    }
    count = await store.delete_by_note("usr_1", "nte_p")
    assert count == 3
    assert set(client.deleted) == {"a1", "a2", "a3"}, "只删 nte_p 的记忆"
    assert set(client.items) == {"b1", "b2"}, "其他 note 的记忆必须原样保留"

    # 枚举始终带 note_id 过滤 + 注入的页大小；3 条 > 页 2 → 必然跨页（≥2 轮）
    assert len(client.get_all_calls) >= 2
    for kwargs in client.get_all_calls:
        assert kwargs["filters"] == {"user_id": "usr_1", "note_id": "nte_p"}
        assert kwargs["top_k"] == 2


async def test_delete_by_note_raises_when_page_never_shrinks(fake_mem0):
    """threads5 P1-1 / INV-3：删除未生效（同批 id 反复取到）时抛
    MemoryEnumerationIncomplete——绝不把「删不动」当成已删完。"""

    class _StuckDeleteMem0(_FakeMem0Memory):
        def delete(self, memory_id):  # 只记录不移除：模拟删除不生效
            self.deleted.append(memory_id)

    fake_mem0.Memory = _StuckDeleteMem0
    store = _make_store(fake_mem0, list_page_size=2)
    client = store._client
    for i in range(3):
        client.items[f"a{i}"] = {"id": f"a{i}", "memory": f"m{i}", "metadata": {"note_id": "nte_p"}}

    with pytest.raises(MemoryEnumerationIncomplete):
        await store.delete_by_note("usr_1", "nte_p")
    assert len(client.get_all_calls) >= 2, "同批重复要靠第二轮取数才能发现"
    assert client.deleted, "发现卡死前确实尝试过删除"


async def test_inmemory_list_page_cap_raises_when_full():
    """INV-3：InMemoryStore 与 Mem0Store 同语义——list() 页上限取满即抛
    MemoryEnumerationIncomplete（进程内字典本不截断，上限让该行为可测）。"""
    from app.memory.inmemory import InMemoryStore

    capped = InMemoryStore(list_page_size=2)
    for text, note in (("甲", "n1"), ("乙", "n1"), ("丙", "n2")):
        await capped.add("usr_1", text, note_id=note)
    with pytest.raises(MemoryEnumerationIncomplete):
        await capped.list("usr_1")

    # 上限高于存量：可证明取全，照常返回
    roomy = InMemoryStore(list_page_size=5)
    await roomy.add("usr_1", "甲", note_id="n1")
    assert len(await roomy.list("usr_1")) == 1


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
