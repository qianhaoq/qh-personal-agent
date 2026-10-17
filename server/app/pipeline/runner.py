"""job 执行器：处理一个/一批待执行 job。worker 与测试共用。"""
import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_sessionmaker
from app.jobs.queue import claim_next, mark_done, mark_failed, mark_note_failed_if_rev
from app.models import Job

logger = logging.getLogger(__name__)


@dataclass
class _Claimed:
    """认领快照：认领事务结束后 job 对象随会话失效，取需要的字段。"""

    id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


async def run_pending_jobs(limit: int = 100, retry_delay_seconds: int = 30) -> int:
    """同步跑完当前所有到期 job（测试与手动触发用）。返回处理个数。

    每个 job 分三步执行：
    1. 认领事务（status=running、attempts+1）
    2. 执行：事务划分由具体 job 实现负责——organize_note 内部是
       “短读事务 → 事务外 LLM → 短写事务 → 提交后写记忆”多个短事务，
       不再把 LLM/记忆写入这类外部调用关在一个大事务里。LLM 失败时
       尚无任何副作用；整理已提交而记忆写入失败时 job 重试，重试发现
       note 已 organized 且 memory_synced=False，只补写记忆不再调 LLM
    3. 收尾事务：成功标 done；失败则退避重试（run_after 推迟），达上限
       标 failed 并把 note.status 置为 failed

    之前把执行放进一个事务是为了失败时整体回滚（曾出现“部分副作用 +
    mark_failed 一起提交”留下脏数据）；副作用拆到提交后补写后不再需要。
    失败的 job 按 run_after 推迟重试（测试可把 retry_delay_seconds 设为 0）。
    """
    maker = get_sessionmaker()
    processed = 0
    while processed < limit:
        claimed = await _claim_one(maker)
        if claimed is None:
            return processed
        processed += 1

        try:
            await execute_job(claimed.kind, claimed.payload)
        except Exception as exc:  # noqa: BLE001 队列要兜住所有错误
            logger.warning("job %s (%s) failed: %s", claimed.id, claimed.kind, exc)
            await _finalize_failure(maker, claimed, str(exc), retry_delay_seconds)
            continue

        async with maker() as session, session.begin():
            job = await session.get(Job, claimed.id)
            if job is not None and job.status == "running":
                await mark_done(session, job)
    return processed


async def _claim_one(maker: async_sessionmaker[AsyncSession]) -> _Claimed | None:
    """事务 1：认领一个到期 job（None 表示队列空）。"""
    async with (
        maker() as session,
        session.begin(),
        claim_next(session) as job,
    ):
        if job is None:
            return None
        return _Claimed(
            id=job.id,
            kind=job.kind,
            payload=json.loads(job.payload_json or "{}"),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
        )


async def _finalize_failure(
    maker: async_sessionmaker[AsyncSession],
    claimed: _Claimed,
    error: str,
    retry_delay_seconds: int,
) -> None:
    """事务 3：失败收尾——退避重试，或达上限标 failed 并置 note 失败。

    note 的失败标记按入队时的 rev 条件更新（mark_note_failed_if_rev）：
    任务在途期间用户已 PATCH（rev 变化，由重新入队的任务负责）或已删除
    时不动 note，不用过期任务的失败覆盖新版本。
    """
    async with maker() as session, session.begin():
        job = await session.get(Job, claimed.id)
        if job is None:
            return
        await mark_failed(session, job, error, retry=True, retry_delay_seconds=retry_delay_seconds)
        if job.status == "failed":
            await mark_note_failed_if_rev(session, claimed.payload)


async def execute_job(kind: str, payload: dict) -> None:
    """执行一个 job。事务划分由具体实现负责（organize_note 用多个短事务，
    见 app.pipeline.organize 的模块 docstring）。"""
    if kind == "organize_note":
        from app.pipeline.organize import organize_note

        await organize_note(payload["note_id"], enqueued_rev=payload.get("rev"))
    elif kind == "cleanup_memory":
        # 删除笔记时入队的幂等清理任务：笔记仍处于已删除才清记忆
        from app.pipeline.organize import cleanup_note_memory

        await cleanup_note_memory(payload["note_id"], payload["user_id"])
    else:
        raise ValueError(f"unknown job kind: {kind}")
