"""公共小工具：ID、sync 序列分配。"""
import secrets
import string

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Sequence

_ALPHABET = string.ascii_lowercase + string.digits


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


async def allocate_seq(session: AsyncSession, name: str = "global", count: int = 1) -> int:
    """原子分配全局单调序列（sync cursor 来源）。首次使用时初始化行。

    必须传入调用方正在用的 session：SQLite 单写者下另开会话会死锁。
    UPDATE 计数器 + RETURNING 在 PostgreSQL 与 SQLite（>=3.35）下都支持。
    """
    row = await session.get(Sequence, name)
    if row is None:
        session.add(Sequence(name=name, value=0))
        await session.flush()
    result = await session.execute(
        update(Sequence)
        .where(Sequence.name == name)
        .values(value=Sequence.value + count)
        .returning(Sequence.value)
    )
    return int(result.scalar_one())
