"""INV-9：一次性建表（init-db）与启动路径不再并发 create_all。"""
import asyncio

from app.db import Base, get_engine
from tests.conftest import IS_POSTGRES, requires_postgres


async def _table_names() -> set[str]:
    engine = get_engine()

    def names(sync_conn):
        from sqlalchemy import inspect

        return set(inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        return await conn.run_sync(names)


async def _drop_all_including_mem0() -> None:
    """清空业务表 + mem0 集合表（后者不在 Base.metadata，drop_all 清不掉）。"""
    from sqlalchemy import text

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        if IS_POSTGRES:
            await conn.execute(text("DROP TABLE IF EXISTS personal_agent_memories CASCADE"))


@requires_postgres
async def test_init_db_concurrent_no_error_pg(client, user):
    """PG 真并发：两个独立连接同时跑 init-db（advisory lock 串行化 DDL），
    均成功、无 duplicate-object 错误，表结构完整。

    仅 PG：并发建表竞态是 PG 多进程部署形态的问题；SQLite 无并发部署场景。
    """
    from app.db import init_db

    # 先清空表，制造“两个进程同时从零建表”的窗口
    await _drop_all_including_mem0()

    results = await asyncio.gather(init_db(), init_db(), return_exceptions=True)
    assert all(r is None for r in results), results
    tables = await _table_names()
    assert {"users", "devices", "notes", "reminders", "jobs", "sequences"} <= tables


async def test_lifespan_skips_create_all_on_postgres(client):
    """PG 下 lifespan 不建表（INV-9：init-db 的职责）；SQLite（本地开发/
    测试）保留启动建表。用计数器 monkeypatch create_all 观察。"""
    import app.db as db_module
    from app.main import lifespan

    calls = {"n": 0}
    real_create_all = db_module.create_all

    async def counting_create_all():
        calls["n"] += 1
        await real_create_all()

    import app.main as main_module

    # startup_create_all 经由 app.db 命名空间调用 create_all
    db_module.create_all = counting_create_all
    try:
        async with lifespan(main_module.app):
            pass
    finally:
        db_module.create_all = real_create_all

    if IS_POSTGRES:
        assert calls["n"] == 0, "PG 下启动不得 create_all（先跑 init-db）"
    else:
        assert calls["n"] == 1, "SQLite 开发/测试路径应保留启动建表"


async def test_init_db_sqlite_creates_tables(client):
    """init-db 在空库上建出全部表（SQLite 直接 create_all；PG 走咨询锁
    事务包装，两者最终表集合一致，此用例两后端都跑）。"""
    from app.db import init_db

    await _drop_all_including_mem0()
    assert await _table_names() == set()

    await init_db()
    tables = await _table_names()
    assert {"users", "devices", "invite_codes", "notes", "reminders", "jobs", "sequences"} <= (
        tables
    )


class _RecordingConn:
    """假连接：记录 execute/run_sync（init_db 的 PG 分支可离线验证）。"""

    def __init__(self, log: list[str]):
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        raw = getattr(stmt, "text", None) or str(stmt)
        self._log.append(raw)

    async def run_sync(self, fn, *args, **kwargs):
        self._log.append(f"run_sync:{getattr(fn, '__qualname__', str(fn))}")


class _RecordingEngine:
    def __init__(self):
        self.log: list[str] = []

    def begin(self):
        return _RecordingConn(self.log)

    async def dispose(self):
        pass


def _fake_initdb_engine(monkeypatch, database_url: str) -> _RecordingEngine:
    import types

    engine = _RecordingEngine()
    monkeypatch.setattr("app.db.create_async_engine", lambda url, **kw: engine)
    monkeypatch.setattr(
        "app.config.get_db_settings",
        lambda: types.SimpleNamespace(database_url=database_url),
    )
    return engine


async def test_init_db_pg_with_mem0_backend_bootstraps_mem0_schema(monkeypatch):
    """P1-3 / INV-9：PG + MEMORY_BACKEND=mem0 时，init-db 在同一咨询锁事务内
    幂等建 mem0 集合（扩展/表/HNSW/GIN，维度随 MEM0_EMBEDDING_DIMS）。
    假引擎离线验证语句与顺序，不依赖本机 vector 扩展。"""
    from app.db import init_db

    monkeypatch.setenv("MEMORY_BACKEND", "mem0")
    monkeypatch.setenv("MEM0_EMBEDDING_DIMS", "3072")
    engine = _fake_initdb_engine(monkeypatch, "postgresql+asyncpg://u:p@db/x")
    await init_db()
    joined = "\n".join(engine.log)
    assert any("pg_advisory_xact_lock" in s for s in engine.log)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in joined
    assert "vector(3072)" in joined, "建表维度须与 MEM0_EMBEDDING_DIMS 一致"
    assert "vector_cosine_ops" in joined  # HNSW
    assert "to_tsvector('simple', payload->>'text_lemmatized')" in joined  # GIN
    create_all_at = next(i for i, s in enumerate(engine.log) if s.startswith("run_sync:"))
    ext_at = next(
        i for i, s in enumerate(engine.log) if "CREATE EXTENSION IF NOT EXISTS vector" in s
    )
    assert create_all_at < ext_at


async def test_init_db_pg_without_mem0_backend_skips_mem0_schema(monkeypatch):
    """P1-3：MEMORY_BACKEND 未设/非 mem0 时不做 mem0 DDL（本地 PG 开发用
    inmemory/disabled 时不引入扩展与权限要求）。"""
    from app.db import init_db

    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    engine = _fake_initdb_engine(monkeypatch, "postgresql+asyncpg://u:p@db/x")
    await init_db()
    joined = "\n".join(engine.log)
    assert any("pg_advisory_xact_lock" in s for s in engine.log)
    assert "CREATE EXTENSION" not in joined
    assert "personal_agent_memories" not in joined


async def test_init_db_sqlite_with_mem0_env_skips_schema_and_does_not_crash(
    client, monkeypatch
):
    """P1-3：SQLite 路径整段跳过 mem0 DDL——本机无 vector 扩展也能跑测试
    套件（MEMORY_BACKEND=mem0 + SQLite 在 Settings 层已被拒，init-db 不
    重复校验也不炸）。"""
    import pytest

    if IS_POSTGRES:
        pytest.skip("本用例断言 SQLite 路径不建 mem0 表；PG 套件另有假引擎用例")
    from app.db import init_db

    monkeypatch.setenv("MEMORY_BACKEND", "mem0")
    await init_db()  # 真实 SQLite 引擎
    assert "personal_agent_memories" not in await _table_names()
