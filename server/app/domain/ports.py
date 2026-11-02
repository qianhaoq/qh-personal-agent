"""领域端口：Engine 与执行方（Worker）之间的协议。

对标 dsh Executor；实现为 OrganizeNoteWorker 与 AnswerAskWorker，结果经 Engine/Channel。
"""
from typing import Protocol

from app.domain.model import WorkItem


class Worker(Protocol):
    """执行一个 WorkItem 并回报结果（不直出信道，结果回 Engine 记状态）。"""

    async def run(self, work_item: WorkItem) -> dict: ...
