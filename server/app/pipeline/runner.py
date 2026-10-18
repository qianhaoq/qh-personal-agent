"""job 执行器：处理一个/一批待执行 job。worker 与测试共用。"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_sessionmaker
from app.errors import safe_error_text
from app.jobs.queue import claim_next, finalize_claim, new_worker_id
from app.timeutil import as_utc

logger = logging.getLogger(__name__)

# 本进程/本 run_pending_jobs 循环默认的 worker 实例 id（jobs.locked_by）。
# 测试可传 worker_id= 模拟另一个 worker 实例（回收/租约校验的并发场景）
_WORKER_ID = new_worker_id()


@dataclass
class _Claimed:
    """认领快照：认领事务结束后 job 对象随会话失效，取需要的字段。

    locked_by/locked_at 是租约凭证，收尾时 finalize_claim 按它们条件校验。
    """

    id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    locked_by: str
    locked_at: datetime


async def run_pending_jobs(
    limit: int = 100, retry_delay_seconds: int = 30, worker_id: str | None = None
) -> int:
    """同步跑完当前所有到期 job（测试与手动触发用）。返回处理个数。

    每个 job 分三步执行：
    1. 认领事务（status=running、attempts+1、写租约 locked_by/locked_at）
    2. 执行：事务划分由具体 job 实现负责——extract 内部是“短读事务 →
       事务外 LLM → 短写事务”，memory_sync 是“短读 → 事务外写记忆 →
       复查/再入队”多个短事务，外部调用（LLM/记忆后端）从不在数据库事务里。
       memory_sync 复查发现笔记状态已变时删掉本次新增的记忆并再入队
       （按 note 幂等收敛，见 app.pipeline.memory_sync）
    3. 收尾事务（带租约校验，见 finalize_claim）：成功标 done；失败则
       退避重试（run_after 推迟），达上限标 failed 并按 kind 分流标记
       note（extract → note.status，memory_sync → memory_status）。
       执行期间任务被回收给别的 worker 时（锁过期），本次收尾放弃——
       job 与 note 都由新持有者决定，不用过期结果覆盖

    之前把执行放进一个事务是为了失败时整体回滚（曾出现“部分副作用 +
    mark_failed 一起提交”留下脏数据）；副作用拆到提交后补写后不再需要。
    失败的 job 按 run_after 推迟重试（测试可把 retry_delay_seconds 设为 0）。
    """
    maker = get_sessionmaker()
    me = worker_id or _WORKER_ID
    processed = 0
    while processed < limit:
        claimed = await _claim_one(maker, me)
        if claimed is None:
            return processed
        processed += 1

        try:
            await execute_job(claimed.kind, claimed.payload, claim=claimed)
        except Exception as exc:  # noqa: BLE001 队列要兜住所有错误
            # INV-14（threads8 P1，runner.py:71）：异常消息可能内嵌用户内容
            # （SQLAlchemy StatementError 的 str 默认带 SQL 绑定参数=笔记
            # 正文），日志与 jobs.last_error 只记类型名+错误码
            summary = safe_error_text(exc)
            logger.warning("job %s (%s) failed: %s", claimed.id, claimed.kind, summary)
            await _finalize(maker, claimed, summary, retry_delay_seconds, me)
            continue

        await _finalize(maker, claimed, None, retry_delay_seconds, me)
    return processed


async def _claim_one(maker: async_sessionmaker[AsyncSession], worker_id: str) -> _Claimed | None:
    """事务 1：认领一个到期 job（None 表示队列空）。

    不用 session.begin() 上下文管理器：claim_next 的判死路径会中途
    commit（INV-1，threads11——判死转移（note/sequences 行锁）提交后才
    找下一个候选），上下文管理器里 commit 后无法继续发语句；这里在
    claim 上下文退出后自行提交领取写入（判死转移已由 queue 内部提交）。
    """
    async with maker() as session:
        async with claim_next(session, worker_id=worker_id) as job:
            if job is None:
                return None
            claimed = _Claimed(
                id=job.id,
                kind=job.kind,
                payload=json.loads(job.payload_json or "{}"),
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                locked_by=job.locked_by or worker_id,
                # SQLite 读回 naive、PG 读回 aware：统一成 aware UTC 再比较
                locked_at=as_utc(job.locked_at),
            )
        await session.commit()
        return claimed


async def _finalize(
    maker: async_sessionmaker[AsyncSession],
    claimed: _Claimed,
    error: str | None,
    retry_delay_seconds: int,
    worker_id: str,
) -> None:
    """事务 3：收尾（带租约校验）。租约失效（被回收）时什么都不写。

    失败标记由 finalize_claim 按 kind 分流（INV-6）：extract 只在 note 仍
    处于入队 rev 时置 note.status=failed；memory_sync 只置
    memory_status=failed（且无其他 pending/running 后继时）。
    """
    async with maker() as session, session.begin():
        await finalize_claim(
            session,
            claimed.id,
            worker_id=worker_id,
            locked_at=claimed.locked_at,
            error=error,
            retry_delay_seconds=retry_delay_seconds,
        )


async def execute_job(kind: str, payload: dict, *, claim: _Claimed | None = None) -> None:
    """执行一个 job。事务划分由具体实现负责（extract / memory_sync 都是
    多个短事务 + 事务外调用，见各自模块的 docstring）。claim 携带租约
    凭证：memory_sync 复查时据此确认自己仍是持有者（threads5 P2-1，
    INV-5 第 3 步/INV-6 的租约栅栏）。"""
    if kind == "extract":
        from app.pipeline.extract import extract_note

        await extract_note(payload["note_id"], enqueued_rev=payload.get("rev"))
    elif kind == "memory_sync":
        from app.pipeline.memory_sync import sync_note_memory

        await sync_note_memory(
            payload["note_id"],
            job_id=claim.id if claim is not None else None,
            worker_id=claim.locked_by if claim is not None else None,
            locked_at=claim.locked_at if claim is not None else None,
        )
    else:
        raise ValueError(f"unknown job kind: {kind}")
