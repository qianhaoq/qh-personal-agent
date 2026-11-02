"""organize_note Worker：Ticket → 现有 pipeline 的 facade。

S2 B 刀起：生产 notes 路径由 Ticket 调度本 facade（API commit 后
schedule_organize → OrganizeNoteWorker.run → extract_note）。notes API
不再 enqueue("extract")；jobs 表主要承载 memory_sync（extract 队列
基础设施保留，供兼容/测试手动入队）。认领后按 job_max_attempts 有界重试；耗尽时条件标记 note failed。
不删 pipeline。
"""
from app.config import get_settings
from app.db import get_sessionmaker
from app.domain.engine import Engine
from app.domain.model import WorkItem
from app.errors import safe_error_text
from app.jobs.queue import mark_note_failed_on_job_failure, new_worker_id


class OrganizeNoteWorker:
    """执行 organize_note WorkItem：调 app.pipeline.extract.extract_note。

    生命周期经 engine 记账（CAS 认领 mark_running → complete/fail）；
    认领失败以 claim_failed 回报且不调 pipeline；异常不上抛，以结果
    dict 回报；extract 在进程内有界重试，耗尽后先按原 rev 条件标记
    note failed，再将 WorkItem 收为 failed。
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    async def run(self, work_item: WorkItem) -> dict:
        worker_id = new_worker_id()
        if not self._engine.mark_running(work_item.id, worker_id=worker_id):
            # 认领失败（已被其它 worker 持有或已终态）：绝不碰 pipeline
            summary = "work item 认领失败（非 pending，可能已被其它 worker 持有）"
            return {
                "work_item_id": work_item.id,
                "status": "claim_failed",
                "error": summary,
            }

        if work_item.note_id is None:
            # 已认领为 running，可安全走 fail 收终态（仅 running 可终态）
            summary = "work item 缺 note_id，无法整理"
            self._engine.fail(work_item.id, summary, worker_id=worker_id)
            return {"work_item_id": work_item.id, "status": "failed", "error": summary}

        # 延迟导入：避免 workers → pipeline 的模块级耦合（与 runner 同风格）
        from app.pipeline.extract import extract_note

        max_attempts = get_settings().job_max_attempts
        for attempt in range(max_attempts):
            try:
                # Ticket 认领上下文不使用 jobs 租约；extract 仍按 note rev 栅栏写回。
                await extract_note(work_item.note_id, enqueued_rev=work_item.note_rev)
            except Exception as exc:  # noqa: BLE001 结果以安全摘要回报，不炸调用方
                summary = safe_error_text(exc)
                if attempt + 1 < max_attempts:
                    continue
                # 外部调用已结束，短事务复用旧 extract 的条件失败收尾语义。
                async with get_sessionmaker()() as session, session.begin():
                    await mark_note_failed_on_job_failure(
                        session,
                        kind="extract",
                        note_id=work_item.note_id,
                        note_rev=work_item.note_rev,
                        job_id=work_item.id,
                    )
                self._engine.fail(work_item.id, summary, worker_id=worker_id)
                return {"work_item_id": work_item.id, "status": "failed", "error": summary}
            else:
                break
        self._engine.complete(
            work_item.id, result={"note_id": work_item.note_id}, worker_id=worker_id
        )
        return {"work_item_id": work_item.id, "status": "done", "error": None}
