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

    加锁顺序规则（全局，防 PG 死锁）：allocate_seq 对 sequences 行取的
    排他锁必须是一个写事务里的**最后一把**锁。调用方要先把全部业务行的
    条件 UPDATE / FOR UPDATE 做完（业务行之间也按固定顺序：note →
    reminders → device …），最后才紧贴提交调用 allocate_seq，再用第二条
    UPDATE 把分配到的 change_seq 写回业务行。反过来（先 allocate_seq
    再去锁业务行）会与其他遵守规则的路径构成 AB-BA 死锁：例如 PATCH
    曾在 .values() 里先 allocate_seq（先锁 sequences 再锁 note），与
    organize 写回的“先 FOR UPDATE note 再 allocate_seq”重叠时 PG 直接
    报 DeadlockDetected。业务行是全新 INSERT（无既有锁可争）时同样先
    插入业务行、后分配 seq，保持全仓一致。

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
