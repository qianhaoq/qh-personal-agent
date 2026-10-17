"""公共小工具：ID、sync 序列分配。"""
import secrets
import string

from sqlalchemy import text, update
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

    提交顺序不变量（/v1/sync 的 cursor 正确性依赖它，见 app/api/sync.py）：
    PG 上这条 UPDATE 对 sequences 行取的排他锁一直持有到事务结束，因此
    拿到小 seq 的写事务必然先于拿到大 seq 的事务提交——任一时刻已提交
    的 seq 集合是全局前缀，未提交写入的 seq 一定大于所有已提交 seq，
    sync 才能放心把页尾 seq 当 cursor。代价是分配 seq 的写事务在提交前
    相互串行（同一时刻至多一个在途写事务），MVP 单用户量级可接受；
    SQLite 本就是单写者，语义相同。不要把这里改成“先读计数再写回”。

    首用初始化必须是 INSERT ... ON CONFLICT DO NOTHING（PG/SQLite 都
    支持）：并发首用时，后到的事务等先者的插入落定后跳过、继续走
    UPDATE。之前的“先 SELECT 没有就裸 INSERT”会让后到者阻塞到先者
    提交再吃 UniqueViolation（并发创建笔记曾因此 500）。
    """
    await session.execute(
        text(
            "INSERT INTO sequences (name, value) VALUES (:name, 0) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        {"name": name},
    )
    result = await session.execute(
        update(Sequence)
        .where(Sequence.name == name)
        .values(value=Sequence.value + count)
        .returning(Sequence.value)
    )
    return int(result.scalar_one())
