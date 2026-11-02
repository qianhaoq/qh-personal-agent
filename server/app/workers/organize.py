"""organize_note Worker：Ticket → 现有 pipeline 的 facade。

第一刀不改现网执行路径：生产上 extract 仍由 jobs 队列驱动
（POST /v1/notes 的 enqueue("extract")），notes 路径不因 Ticket 再
enqueue 一次（不双跑）。本 facade 供 Engine 路径的 Assignment 调用，
单测用它验证「领域骨架能驱动 pipeline」；pipeline 稳定收口进 Engine
后再删 facade（mapping §6.3 过渡策略）。
"""
from app.domain.engine import Engine
from app.domain.model import WorkItem
from app.errors import safe_error_text
from app.jobs.queue import new_worker_id


class OrganizeNoteWorker:
    """执行 organize_note WorkItem：调 app.pipeline.extract.extract_note。

    生命周期经 engine 记账（CAS 认领 mark_running → complete/fail）；
    认领失败以 claim_failed 回报且不调 pipeline；异常不上抛，以结果
    dict 回报（重试编排属后续刀的 Effects，v0 不在此实现）。
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

        try:
            # 延迟导入：避免 workers → pipeline 的模块级耦合（与 runner 同风格）
            from app.pipeline.extract import extract_note

            # 无租约凭证调用（job_id=None）：facade 路径不在 jobs 队列的
            # 认领上下文里，按 extract_note 文档的「无栅栏」语义处理
            await extract_note(work_item.note_id, enqueued_rev=work_item.note_rev)
        except Exception as exc:  # noqa: BLE001 结果以安全摘要回报，不炸调用方
            summary = safe_error_text(exc)
            self._engine.fail(work_item.id, summary, worker_id=worker_id)
            return {"work_item_id": work_item.id, "status": "failed", "error": summary}
        self._engine.complete(
            work_item.id, result={"note_id": work_item.note_id}, worker_id=worker_id
        )
        return {"work_item_id": work_item.id, "status": "done", "error": None}
