"""job 执行器：处理一个/一批待执行 job。worker 与测试共用。"""
import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common import allocate_seq
from app.db import get_sessionmaker
from app.jobs.queue import claim_next, mark_done, mark_failed
from app.models import Job, Note
from app.timeutil import utcnow

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

    每个 job 分三个事务执行：
    1. 认领（status=running、attempts+1）
    2. 执行整理的全部副作用；抛异常则该事务整体回滚，不留半成品
    3. 收尾：成功标 done；失败则退避重试（run_after 推迟），达上限标
       failed 并把 note.status 置为 failed

    之前把三步放在同一个事务里，执行失败时“部分副作用 + mark_failed”
    会一起提交，留下 organizing 状态的脏数据。
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
            async with maker() as session, session.begin():
                await execute_job(session, claimed.kind, claimed.payload)
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
    """事务 3：失败收尾——退避重试，或达上限标 failed 并置 note 失败。"""
    async with maker() as session, session.begin():
        job = await session.get(Job, claimed.id)
        if job is None:
            return
        await mark_failed(session, job, error, retry=True, retry_delay_seconds=retry_delay_seconds)
        if job.status == "failed":
            await _mark_note_failed(session, claimed.payload)


async def execute_job(session: AsyncSession, kind: str, payload: dict) -> None:
    if kind == "organize_note":
        from app.pipeline.organize import organize_note

        await organize_note(session, payload["note_id"], enqueued_rev=payload.get("rev"))
    else:
        raise ValueError(f"unknown job kind: {kind}")


async def _mark_note_failed(session: AsyncSession, payload: dict) -> None:
    note_id = payload.get("note_id")
    if not note_id:
        return
    note = await session.get(Note, note_id)
    if note is not None:
        note.status = "failed"
        note.change_seq = await allocate_seq(session)
        note.updated_at = utcnow()
