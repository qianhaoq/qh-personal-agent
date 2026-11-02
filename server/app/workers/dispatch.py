"""同进程调度 OrganizeNoteWorker（S2 B 刀：Ticket → Worker，不经 jobs extract）。

API 在 commit + submit_note_uploaded 开单成功后 create_task 跑 Worker；
不在请求事务内调 LLM。测试用 drain_organize_tasks 等待登记中的 task。
"""
from __future__ import annotations

import asyncio
import logging

from app.domain.engine import get_engine
from app.domain.model import WorkItem
from app.errors import safe_error_text

logger = logging.getLogger(__name__)

# 进程内登记：供测试 drain；完成/取消后自动 discard
_pending: set[asyncio.Task] = set()


def schedule_organize(work_item: WorkItem) -> asyncio.Task | None:
    """开单成功后调度 Worker；无 running loop 时只记日志并返回 None。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "schedule_organize: 无 running event loop，跳过 work_item=%s",
            work_item.id,
        )
        return None

    async def _run() -> None:
        # 延迟导入：避免 workers 包初始化环
        from app.workers.organize import OrganizeNoteWorker

        try:
            await OrganizeNoteWorker(get_engine()).run(work_item)
        except Exception as exc:  # noqa: BLE001 旁路不得影响 /v1
            logger.warning(
                "OrganizeNoteWorker 失败 work_item=%s: %s",
                work_item.id,
                safe_error_text(exc),
            )

    task = loop.create_task(_run(), name=f"organize:{work_item.id}")
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain_organize_tasks() -> None:
    """等待当前已登记的 organize tasks（测试夹具用）。"""
    pending = [t for t in list(_pending) if not t.done()]
    if not pending:
        return
    await asyncio.gather(*pending, return_exceptions=True)
