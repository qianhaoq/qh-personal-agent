"""job 执行器：处理一个/一批待执行 job。worker 与测试共用。"""
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.common import allocate_seq
from app.db import get_sessionmaker
from app.jobs.queue import claim_next, mark_done, mark_failed
from app.models import Note
from app.timeutil import utcnow

logger = logging.getLogger(__name__)


async def run_pending_jobs(limit: int = 100, retry_delay_seconds: int = 30) -> int:
    """同步跑完当前所有到期 job（测试与手动触发用）。返回处理个数。

    失败的 job 按 run_after 推迟重试（测试可把 retry_delay_seconds 设为 0）。
    """
    maker = get_sessionmaker()
    processed = 0
    async with maker() as session:
        while processed < limit:
            async with session.begin(), claim_next(session) as job:
                if job is None:
                    return processed
                payload = json.loads(job.payload_json or "{}")
                try:
                    await execute_job(session, job.kind, payload)
                    await mark_done(session, job)
                except Exception as exc:  # noqa: BLE001 队列要兜住所有错误
                    logger.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
                    await mark_failed(
                        session, job, str(exc), retry=True, retry_delay_seconds=retry_delay_seconds
                    )
                    if job.status == "failed":
                        await _mark_note_failed(session, payload)
            processed += 1
    return processed


async def execute_job(session: AsyncSession, kind: str, payload: dict) -> None:
    if kind == "organize_note":
        from app.pipeline.organize import organize_note

        await organize_note(session, payload["note_id"])
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
