"""真实 mem0ai==2.2.1 + pgvector 集成测试（threads6 可选验证项，独立 venv 运行）。

在真实 mem0ai 与真实 pgvector 上验证适配层（app/memory/mem0_store.py）的
两个关键行为——fake client 测不出的部分：

1. **构造配置真的能被 mem0 2.2.1 接受**：llm/embedder 的 OpenAI 兼容配置
   键是 `openai_base_url`（传 `base_url` 构造直接 TypeError——本测试首次
   运行时抓到的真实缺陷，fake 的 from_config 收任意 dict 发现不了）；
2. **user_id / note_id 过滤下推**：search/get_all 的 filters、delete_by_note
   的 metadata 过滤（`payload->>'note_id'`）不串用户、不串笔记；
3. **无游标分页下的穷尽删除**：get_all 只有 top_k 截断，小页上限
   （list_page_size=3）下「边取边删」循环也能删净超过一页的数据；
4. **add 必须 infer=False（Codex P1 / INV-3）**：mem0 默认 infer=True 的
   add 会基于用户范围内既有记忆做 UPDATE/DELETE/NOOP 决策，相似内容会
   改写/复用其他 note 的记忆——两条相似内容先后 add 后，先写 note 的
   记忆条数与内容必须原样保留（归属隔离）。

确定性本地 embedder：把 EmbedderFactory 的 openai 槽位替换成 crc32 token
哈希的 1536 维向量（pgvector 表维度默认 1536，必须匹配）——不访问任何
外部 API。LLM 不参与：add 全程 infer=False（适配层策略，见上第 4 点），
绕过 mem0 的 LLM 抽取/改写决策；凭据是占位值，只在构造时实例化 OpenAI
客户端，绝不发起调用。

运行方式（独立 venv，避免污染常规 civenv）：

    python3.13 -m venv /workspace/.agent-tmp/mem0venv
    /workspace/.agent-tmp/mem0venv/bin/pip install \\
        -r server/requirements.txt -r server/requirements-mem0.txt \\
        -r server/requirements-dev.txt
    cd server && TEST_DATABASE_URL=postgresql+asyncpg://pa:...@localhost/pa_test \\
        /workspace/.agent-tmp/mem0venv/bin/python -m pytest \\
        tests/test_mem0_integration.py -q

跳过条件（各自带原因，常规环境全跳过、不算失败）：
- mem0 未安装（本机按 README 只装 requirements-dev.txt，不含 mem0ai）；
- TEST_DATABASE_URL 未设置（pgvector 需要 PostgreSQL 目标库）；
- 目标库未安装 vector 扩展（需要数据库超级用户执行
  `sudo -u postgres psql <db> -c 'CREATE EXTENSION IF NOT EXISTS vector'`）；
- TEST_DATABASE_URL 无法连接（数据库未起）。
"""
import math
import os
import re
import uuid
import zlib

import pytest

pytest.importorskip(
    "mem0",
    reason="未安装 mem0ai：本文件验证真实后端，请用 requirements-mem0.txt 的独立 venv 运行（见模块 docstring）",
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if not TEST_DATABASE_URL:
    pytest.skip(
        "TEST_DATABASE_URL 未设置：pgvector 集成需要 PostgreSQL 目标库", allow_module_level=True
    )
if not TEST_DATABASE_URL.startswith("postgresql"):
    pytest.skip(
        f"TEST_DATABASE_URL 不是 postgresql URL：{TEST_DATABASE_URL!r}", allow_module_level=True
    )


def _vector_extension_installed() -> bool:
    # 与 mem0 pgvector 后端同一套驱动顺序（先 psycopg3 再 psycopg2）
    try:
        import psycopg as pg_driver
    except ImportError:
        import psycopg2 as pg_driver

    from app.memory.mem0_store import _pg_connection_string

    conn = pg_driver.connect(_pg_connection_string(TEST_DATABASE_URL))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            return cur.fetchone() is not None
    finally:
        conn.close()


try:
    _VECTOR_OK = _vector_extension_installed()
except Exception as exc:  # noqa: BLE001 - 连不上/无权限都按环境不具备跳过
    pytest.skip(f"无法连接 TEST_DATABASE_URL 检查 vector 扩展：{exc}", allow_module_level=True)
if not _VECTOR_OK:
    pytest.skip(
        "目标库未安装 vector 扩展：sudo -u postgres psql <db> "
        "-c 'CREATE EXTENSION IF NOT EXISTS vector'",
        allow_module_level=True,
    )


# pgvector 表维度：Mem0Store 的 vector_store 配置不覆盖 embedding_model_dims，
# mem0 默认建 vector(1536) 列——确定性 embedder 的输出维度必须一致
_DIM = 1536


class _DeterministicEmbedding:
    """crc32 token 哈希 + L2 归一的确定性向量：完全本地，无外部 API。
    同 token 集合 → 同向量；查询与存储共用，search 才有可比性。"""

    def __init__(self, config) -> None:
        self.config = config

    def embed(self, text, memory_action=None):
        vec = [0.0] * _DIM
        for token in re.findall(r"\w+", str(text)):
            vec[zlib.crc32(token.encode("utf-8")) % _DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def store(monkeypatch, tmp_path, request):
    """真实 Mem0Store：mem0ai + pgvector + 确定性本地 embedder。

    页大小默认 3（逼出「边取边删」的多轮推进路径）；用
    `@pytest.mark.parametrize("store", [50], indirect=True)` 放大。

    - EmbedderFactory 的 openai 槽位换成 _DeterministicEmbedding（适配层
      配置里 embedder.provider 是 openai，替换槽位即可，无需改适配层）；
    - HOME 重定向到 tmp_path：mem0 的 history.db 落在临时目录，不污染 home；
    - MEM0_TELEMETRY=False：不向 PostHog 发任何遥测；
    - LLM 凭据是占位值：只在构造时实例化 OpenAI 客户端，绝不发起调用
      （种子数据 infer=False，不走 infer 抽取）；
    - 结束后 get_settings.cache_clear()：恢复 env（monkeypatch）之后，
      缓存不能留着指向测试专用的配置。
    """
    from app.config import get_settings

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_TELEMETRY", "False")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_BACKEND", "mem0")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-integration-local-only")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_MODEL", "integration-dummy")
    get_settings.cache_clear()

    from mem0.utils.factory import EmbedderFactory

    monkeypatch.setitem(
        EmbedderFactory.provider_to_class,
        "openai",
        f"{__name__}._DeterministicEmbedding",
    )

    from app.memory.mem0_store import Mem0Store

    try:
        yield Mem0Store(list_page_size=getattr(request, "param", 3))
    finally:
        get_settings.cache_clear()
        _drop_mem0_tables()


def _drop_mem0_tables() -> None:
    """清掉本文件建的 mem0 collection 表：它不属于应用的 Base metadata，
    drop_all 不碰它，而 test_initdb 断言空库表集合——测试必须把库原样
    交还（每次运行用唯一 user_id 已保证数据不串，这里保证 schema 不残留）。"""
    try:
        import psycopg as pg_driver
    except ImportError:
        import psycopg2 as pg_driver

    from app.memory.mem0_store import _pg_connection_string

    conn = pg_driver.connect(_pg_connection_string(TEST_DATABASE_URL))
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS personal_agent_memories")
        conn.commit()
    finally:
        conn.close()


def _fresh_user() -> str:
    # collection 跨运行持久存在：每次运行用独立 user_id，杜绝历史残留
    return f"usr_it_{uuid.uuid4().hex[:10]}"


async def _seed(store, user_id: str, entries: list[tuple[str, str | None]]) -> list[str]:
    """直写原始文本（infer=False，不走 LLM），返回全部 memory id。"""
    import asyncio

    ids = []
    for text, note_id in entries:
        result = await asyncio.to_thread(
            store._client.add,
            text,
            user_id=user_id,
            metadata={"note_id": note_id},
            infer=False,
        )
        raw = result.get("results", []) if isinstance(result, dict) else result
        ids.extend(r["id"] for r in raw)
    return ids


async def _cleanup(store, *user_ids: str) -> None:
    # 清理尽力而为（collection 跨运行持久存在，唯一 user_id 兜底），不遮蔽断言
    for uid in user_ids:
        try:
            await store.delete_all(uid)
        except Exception:  # noqa: BLE001, S110
            pass


async def test_add_infer_false_never_rewrites_other_notes_memory(store):
    """Codex P1 / INV-3：适配层 add 必须 infer=False——mem0 默认 infer=True
    会先检索用户范围内既有记忆、由 LLM 决定 ADD/UPDATE/DELETE/NOOP，相似
    内容会被判成「相关」而改写/复用，破坏 note_id/source_rev 归属。两条
    相似内容（B 的文本是 A 的超集）先后经 store.add 写入后，按 note_id
    过滤：A 的记忆条数与内容不变，B 的记忆归属 B。infer=False 全程不碰
    LLM（占位 LLM 不可达也不会被调用）。"""
    import asyncio

    user = _fresh_user()
    note_a, note_b = "nte_it_infer_a", "nte_it_infer_b"
    text_a = "对花生和腰果严重过敏"
    text_b = "对花生和腰果严重过敏，最近吃芒果也起疹子"
    try:
        added_a = await store.add(user, text_a, note_id=note_a, source_rev=1)
        added_b = await store.add(user, text_b, note_id=note_b, source_rev=2)
        # infer=False 一次 add 恰好写一条本 note 的记忆
        assert len(added_a) == 1 and len(added_b) == 1
        assert added_a[0].note_id == note_a and added_b[0].note_id == note_b

        async def by_note(note_id: str) -> list[dict]:
            # pgvector 的 metadata 键下推（payload->>'note_id'）按 note 精确取
            result = await asyncio.to_thread(
                store._client.get_all,
                filters={"user_id": user, "note_id": note_id},
                top_k=10,
            )
            return result["results"]

        mems_a = await by_note(note_a)
        mems_b = await by_note(note_b)
        assert [m["memory"] for m in mems_a] == [text_a], "A 的记忆内容必须原样保留"
        assert mems_a[0]["metadata"]["note_id"] == note_a
        assert int(mems_a[0]["metadata"]["source_rev"]) == 1
        assert [m["memory"] for m in mems_b] == [text_b]
        assert int(mems_b[0]["metadata"]["source_rev"]) == 2
        # 用户名下恰好两条：infer=True 的 NOOP/合并会让总数 < 2
        assert len(await store.list(user)) == 2
    finally:
        await _cleanup(store, user)


@pytest.mark.parametrize("store", [50], indirect=True)
async def test_mem0_constructs_and_scopes_user_and_note(store):
    """真实 mem0 + pgvector：search/get_all 的 user_id 过滤下推（同文本
    跨用户不串），delete_by_note 只删 (user, note) 的记忆、别的笔记与
    别的用户不受影响（payload->>'note_id' 过滤）。"""
    user_a, user_b = _fresh_user(), _fresh_user()
    note_a1, note_a2, note_b1 = "nte_it_a1", "nte_it_a2", "nte_it_b1"
    try:
        await _seed(
            store,
            user_a,
            [("对花生过敏", note_a1), ("喜欢周末爬山", note_a1), ("每周五有站会", note_a2)],
        )
        # 同文本落在另一个用户名下：向量相同，必须靠 user_id 过滤分开
        await _seed(store, user_b, [("对花生过敏", note_b1)])

        # search 按 user 隔离
        hits_a = await store.search(user_a, "对花生过敏", top_k=5)
        assert [m.text for m in hits_a if m.text == "对花生过敏"], "应命中自己的记忆"
        assert all(m.user_id == user_a for m in hits_a)
        hits_b = await store.search(user_b, "对花生过敏", top_k=5)
        assert [m.text for m in hits_b if m.text == "对花生过敏"]
        assert all(m.user_id == user_b for m in hits_b)

        # list 按 user 隔离，note_id 随 metadata 回读
        items_a = sorted(await store.list(user_a), key=lambda m: m.text)
        assert len(items_a) == 3
        assert {m.note_id for m in items_a} == {note_a1, note_a2}
        assert len(await store.list(user_b)) == 1

        # delete_by_note 只删 (user_a, note_a1)
        deleted = await store.delete_by_note(user_a, note_a1)
        assert deleted == 2, "应恰好删掉该笔记的两条记忆"
        remaining = sorted(await store.list(user_a), key=lambda m: m.text)
        assert [m.text for m in remaining] == ["每周五有站会"]
        assert remaining[0].note_id == note_a2
        assert len(await store.list(user_b)) == 1, "别的用户不受影响"
    finally:
        await _cleanup(store, user_a, user_b)


async def test_mem0_delete_by_note_paginates_past_page_cap(store):
    """get_all 无游标分页：单笔记记忆数（7）超过页上限（3）时，list()
    按不完整报错（MemoryEnumerationIncomplete），delete_by_note 的
    「边取边删」循环仍能穷尽删净。"""
    from app.memory.base import MemoryEnumerationIncomplete

    user = _fresh_user()
    note = "nte_it_page"
    try:
        ids = await _seed(
            store, user, [(f"第 {i} 条集成测试记忆", note) for i in range(1, 8)]
        )
        assert len(ids) == 7

        with pytest.raises(MemoryEnumerationIncomplete):
            await store.list(user)

        deleted = await store.delete_by_note(user, note)
        assert deleted == 7, "小页上限下也必须删净（每轮删掉本轮取到的）"
        assert await store.list(user) == []
        assert await store.search(user, "集成测试", top_k=5) == []
    finally:
        await _cleanup(store, user)
