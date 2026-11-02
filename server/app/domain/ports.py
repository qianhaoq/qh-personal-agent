"""领域端口：Engine 与执行方（Worker）之间的协议。

对标 dsh 的 Executor 端口；本仓 v0 的实现是 workers/organize 的
facade（调现有 pipeline），不引入外部执行环境。
"""
from typing import Protocol

from app.domain.model import WorkItem


class Worker(Protocol):
    """执行一个 WorkItem 并回报结果（不直出信道，结果回 Engine 记状态）。"""

    async def run(self, work_item: WorkItem) -> dict: ...
