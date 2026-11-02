"""Worker 包：WorkItem 的执行方（对标 dsh Executor，域内任务执行器）。

边界（映射 §3 规则 2）：workers 不直出信道（不推通知/UI），结果回
engine 记状态，由 Channel 侧（后续刀的 sync/actions[]）消费。
"""
from app.workers.dispatch import drain_organize_tasks, schedule_organize
from app.workers.organize import OrganizeNoteWorker

__all__ = ["OrganizeNoteWorker", "schedule_organize", "drain_organize_tasks"]
