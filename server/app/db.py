"""SQLAlchemy 2 async 引擎与会话。"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_sessionmaker = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    return _sessionmaker


async def get_db_session():
    """FastAPI 依赖：每请求一个会话。"""
    async with get_sessionmaker()() as session:
        yield session


async def create_all() -> None:
    """启动时建表。S1 用 create_all，后续换 Alembic（见 README）。"""
    from app import models  # noqa: F401 确保模型已注册

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# init-db 的并发互斥键（任意固定 bigint，只属于本服务）
_INIT_DB_ADVISORY_KEY = 918273645070


# mem0 pgvector 集合的幂等引导 DDL（P1-3 / INV-9）：mem0ai 首次操作时懒建
# schema（CREATE EXTENSION vector + 建 personal_agent_memories 表与 HNSW/
# GIN 索引）——多 worker 并发首跑竞态，且受限角色可能没有建扩展权限。这里
# 在 init-db 的咨询锁事务内一次性幂等建好，请求路径（api/worker 运行期）
# 绝不做 DDL。对齐 mem0ai==2.2.1 默认 collection_name=personal_agent_memories、
# embedding_model_dims=1536、hnsw=True。


def _mem0_embedding_dims() -> int:
    """init-db 侧的向量维度：经 Mem0MigrateSettings 读 .env / 环境变量
    （INV-9 分层，不触发完整 Settings）。缺省/非法回落 1536（mem0ai 2.2.1
    默认）；上界 2000 对齐 pgvector HNSW opclass。非法值会在 api/worker 的
    完整 Settings 校验报错，不影响建表。"""
    from app.config import get_mem0_migrate_settings

    dims = get_mem0_migrate_settings().mem0_embedding_dims
    try:
        dims = int(dims)
    except (TypeError, ValueError):
        return 1536
    return dims if 0 < dims <= 2000 else 1536


def _mem0_schema_statements() -> tuple[str, ...]:
    dims = _mem0_embedding_dims()
    return (
        "CREATE EXTENSION IF NOT EXISTS vector",
        (
            "CREATE TABLE IF NOT EXISTS personal_agent_memories (\n"
            "  id UUID PRIMARY KEY,\n"
            f"  vector vector({dims}),\n"
            "  payload JSONB\n"
            ")"
        ),
        (
            "CREATE INDEX IF NOT EXISTS personal_agent_memories_hnsw_idx\n"
            "  ON personal_agent_memories USING hnsw (vector vector_cosine_ops)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS personal_agent_memories_text_lemmatized_idx\n"
            "  ON personal_agent_memories USING gin "
            "(to_tsvector('simple', payload->>'text_lemmatized'))"
        ),
    )


async def _ensure_mem0_schema(conn: AsyncConnection) -> None:
    """当前事务内幂等建 mem0 集合（语句见 _mem0_schema_statements）。"""
    for stmt in _mem0_schema_statements():
        await conn.execute(text(stmt))




async def init_db() -> None:
    """一次性建表：python -m app.cli init-db（INV-9，修 C6）。

    PG 下在单个事务内先 pg_advisory_xact_lock 再 create_all：DDL 事务性 +
    咨询锁，多进程/并发执行安全且串行化（api/worker 启动时不再各自建表，
    docker-compose 由 one-shot migrate 服务先跑本命令）。SQLite 直接
    create_all（单文件库无并发部署场景）。

    只加载 DbSettings（数据库层，threads7 P1 / INV-9 校验分层）：自建
    引擎、用完即 dispose，不经 get_engine/get_settings——建表不依赖
    LLM/记忆后端/JWT 配置，也不被它们的生产校验挡住（tests/
    test_compose.py::test_init_db_does_not_load_full_settings 毒化验证）。
    """
    from app import models  # noqa: F401 确保模型已注册
    from app.config import get_db_settings

    database_url = get_db_settings().database_url
    engine = create_async_engine(database_url, pool_pre_ping=True, future=True)
    try:
        if database_url.startswith("postgresql"):
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_DB_ADVISORY_KEY}
                )
                await conn.run_sync(Base.metadata.create_all)
                # mem0 集合引导（P1-3）：仅 MEMORY_BACKEND=mem0（compose 的
                # migrate 服务显式置位，或仓库 .env）；与 create_all 同事务
                # 同一把咨询锁，幂等可重入。经 Mem0MigrateSettings 读
                # .env/环境变量，不触发完整 Settings 校验（INV-9 分层）
                from app.config import get_mem0_migrate_settings

                if get_mem0_migrate_settings().memory_backend == "mem0":
                    await _ensure_mem0_schema(conn)
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def startup_create_all() -> None:
    """api/worker 启动路径：SQLite（本地开发/测试）建表；PG 必须先跑
    init-db（表缺失时让首个查询快速失败，暴露部署错误），这里不建表。"""
    if get_settings().database_url.startswith("postgresql"):
        return
    await create_all()  # invariant-ok[INV-STARTUP-DDL]: SQLite dev/test only; PG returns above and requires `python -m app.cli init-db`


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
