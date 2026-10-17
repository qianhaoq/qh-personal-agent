"""SQLAlchemy 2 async 引擎与会话。"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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


async def init_db() -> None:
    """一次性建表：python -m app.cli init-db（INV-9，修 C6）。

    PG 下在单个事务内先 pg_advisory_xact_lock 再 create_all：DDL 事务性 +
    咨询锁，多进程/并发执行安全且串行化（api/worker 启动时不再各自建表，
    docker-compose 由 one-shot migrate 服务先跑本命令）。SQLite 直接
    create_all（单文件库无并发部署场景）。
    """
    from app import models  # noqa: F401 确保模型已注册

    engine = get_engine()
    if get_settings().database_url.startswith("postgresql"):
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_DB_ADVISORY_KEY}
            )
            await conn.run_sync(Base.metadata.create_all)
        return
    await create_all()


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
