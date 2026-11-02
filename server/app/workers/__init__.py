"""Worker 包：WorkItem 的执行方（对标 dsh Executor，域内任务执行器）。

边界（映射 §3 规则 2）：workers 不直出信道（不推通知/UI），结果回
Engine，由 Channel 在同一个 fenced 事务里落库或返回持久结果。
"""
from app.workers.organize import OrganizeNoteWorker

__all__ = ["OrganizeNoteWorker"]
