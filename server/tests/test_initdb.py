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


@requires_postgres
async def test_init_db_concurrent_no_error_pg(client, user):
    """PG 真并发：两个独立连接同时跑 init-db（advisory lock 串行化 DDL），
    均成功、无 duplicate-object 错误，表结构完整。

    仅 PG：并发建表竞态是 PG 多进程部署形态的问题；SQLite 无并发部署场景。
    """
    from app.db import init_db

    # 先清空表，制造“两个进程同时从零建表”的窗口
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

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

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    assert await _table_names() == set()

    await init_db()
    tables = await _table_names()
    assert {"users", "devices", "invite_codes", "notes", "reminders", "jobs", "sequences"} <= (
        tables
    )
