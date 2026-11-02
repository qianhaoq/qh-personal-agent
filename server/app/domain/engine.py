"""领域唯一写入口（对标 dsh src/core 的 Engine）。

receive(trigger) 是轻量、同步、纯内存的：去重 → 记 Trigger/Thread →
Eval（policy.evaluate，规则无 LLM）→ accept 则签 Mandate 并开 WorkItem
（pending）。不碰数据库、不调外部服务，可在 API 事务外安全调用
（docs/server-invariants.md INV-1.4：事务内不调 LLM/外部服务——本引擎
根本没有这类调用）。

与现网执行路径的关系（第一刀双路径，见 docs/architecture-superagent.md）：
- 现网（S2 B 刀起）：POST/PATCH /v1/notes 开 Ticket 后同进程调度
  OrganizeNoteWorker → pipeline.extract；jobs 主要剩 memory_sync；
- 旁路：engine 开出的 WorkItem 由 workers/organize 的 facade 消化
  （单测验证领域骨架可驱动 pipeline；生产 notes 路径不因 Ticket
  再 enqueue 一次 extract）。

v0 内存实现：进程内可见、重启即空；PG 持久化（work_items/triggers 表）
留给后续刀。
"""
from functools import lru_cache

from app.common import new_id
from app.domain.model import (
    TRIGGER_NOTE_UPLOADED,
    WORK_ITEM_DONE,
    WORK_ITEM_FAILED,
    WORK_ITEM_ORGANIZE_NOTE,
    WORK_ITEM_PENDING,
    WORK_ITEM_RUNNING,
    DomainEvent,
    Receipt,
    Thread,
    Trigger,
    WorkItem,
)
from app.policy import PersonalPolicy
from app.timeutil import utcnow


class Engine:
    """Trigger → Thread → Eval → Mandate → WorkItem 的内存编排。"""

    def __init__(self, policy: PersonalPolicy | None = None):
        self._policy = policy or PersonalPolicy()
        self._triggers: dict[str, Trigger] = {}
        self._threads: dict[str, Thread] = {}
        self._work_items: dict[str, WorkItem] = {}
        # dedupe_key → 已处理的 trigger id（幂等）
        self._by_dedupe: dict[str, str] = {}
        # trigger id → 开出的 WorkItem id（duplicate 回执用）
        self._ticket_by_trigger: dict[str, str] = {}
        self._events: list[DomainEvent] = []

    @property
    def policy(self) -> PersonalPolicy:
        return self._policy

    # ------------------------------------------------------------ receive
    def receive(self, trigger: Trigger) -> Receipt:
        """接收一个入口事件；幂等：同 dedupe_key 重放返回 duplicate 回执。"""
        seen = self._by_dedupe.get(trigger.dedupe_key)
        if seen is not None:
            work_item = self._work_items.get(self._ticket_by_trigger.get(seen, ""))
            return Receipt(trigger_id=seen, duplicate=True, decision=None, work_item=work_item)

        self._triggers[trigger.id] = trigger
        self._by_dedupe[trigger.dedupe_key] = trigger.id
        self._thread(trigger.thread_key).trigger_ids.append(trigger.id)
        self._record("trigger_recorded", None, f"{trigger.kind} from {trigger.source}")

        decision = self._policy.evaluate(trigger)
        if decision.outcome != "accept":
            self._record("decision_ignored", None, f"{trigger.kind}: {decision.reason}")
            return Receipt(trigger_id=trigger.id, duplicate=False, decision=decision, work_item=None)

        now = utcnow()
        work_item = WorkItem(
            id=new_id("wki"),
            kind=self._work_item_kind(trigger),
            thread_key=trigger.thread_key,
            trigger_dedupe_key=trigger.dedupe_key,
            user_id=trigger.user_id,
            mandate=self._policy.mandate(trigger),
            decision=decision,
            status=WORK_ITEM_PENDING,
            created_at=now,
            updated_at=now,
            note_id=trigger.payload.get("note_id"),
            note_rev=trigger.payload.get("rev"),
        )
        self._work_items[work_item.id] = work_item
        self._ticket_by_trigger[trigger.id] = work_item.id
        self._record("work_item_opened", work_item.id, f"{work_item.kind} for {trigger.kind}")
        return Receipt(trigger_id=trigger.id, duplicate=False, decision=decision, work_item=work_item)

    # ------------------------------------------------------- 生命周期
    def mark_running(self, work_item_id: str, *, worker_id: str) -> bool:
        """Assignment：WorkItem 派给 worker 执行（pending → running）。

        CAS 认领：仅当 status==pending 才生效；已 running/done/failed 的
        二次认领一律拒绝（返回 False，无副作用）。未知 id 仍 raise。
        """
        item = self._require(work_item_id)
        if item.status != WORK_ITEM_PENDING:
            return False
        item.status = WORK_ITEM_RUNNING
        item.worker_id = worker_id
        self._touch(item)
        self._record("work_item_running", item.id, f"worker {worker_id}")
        return True

    def complete(
        self, work_item_id: str, result: dict | None = None, *, worker_id: str
    ) -> bool:
        """Worker report ready：running → done（仅持牌 worker）。

        CAS：status==running 且 worker_id 匹配才生效；否则 no-op 返回
        False（done/failed 终态不可覆盖，非持有者不可回报）。
        """
        item = self._require(work_item_id)
        if item.status != WORK_ITEM_RUNNING or item.worker_id != worker_id:
            return False
        item.status = WORK_ITEM_DONE
        item.result = result
        self._touch(item)
        self._record("work_item_completed", item.id, item.kind)
        return True

    def fail(self, work_item_id: str, error: str, *, worker_id: str) -> bool:
        """Worker report failed：running → failed（error 只存安全摘要）。

        CAS：status==running 且 worker_id 匹配才生效；否则 no-op 返回
        False（禁止 done → failed）。
        """
        item = self._require(work_item_id)
        if item.status != WORK_ITEM_RUNNING or item.worker_id != worker_id:
            return False
        item.status = WORK_ITEM_FAILED
        item.error = error
        self._touch(item)
        self._record("work_item_failed", item.id, item.kind)
        return True

    # ------------------------------------------------------------ 查询
    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        return self._work_items.get(work_item_id)

    def get_trigger(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def work_items_for_note(self, note_id: str) -> list[WorkItem]:
        return [w for w in self._work_items.values() if w.note_id == note_id]

    def events(self) -> list[DomainEvent]:
        return list(self._events)

    # ------------------------------------------------------------ 内部
    def _thread(self, thread_key: str) -> Thread:
        thread = self._threads.get(thread_key)
        if thread is None:
            thread = Thread(key=thread_key, created_at=utcnow())
            self._threads[thread_key] = thread
        return thread

    def _work_item_kind(self, trigger: Trigger) -> str:
        # v0 唯一映射：笔记上传 → 整理。后续刀扩 answer_ask / digest
        return WORK_ITEM_ORGANIZE_NOTE if trigger.kind == TRIGGER_NOTE_UPLOADED else "unknown"

    def _require(self, work_item_id: str) -> WorkItem:
        item = self._work_items.get(work_item_id)
        if item is None:
            raise ValueError(f"unknown work item: {work_item_id}")
        return item

    def _touch(self, item: WorkItem) -> None:
        item.updated_at = utcnow()

    def _record(self, type_: str, work_item_id: str | None, detail: str) -> None:
        self._events.append(
            DomainEvent(
                seq=len(self._events) + 1,
                type=type_,
                at=utcnow(),
                work_item_id=work_item_id,
                detail=detail,
            )
        )


@lru_cache
def get_engine() -> Engine:
    """进程内共享引擎（API 钩子用）。多进程部署下各进程独立——v0 已知
    限制，见 docs/architecture-superagent.md 的「不构成承诺」一节。"""
    return Engine()


def reset_engine() -> None:
    """测试/运维：丢弃进程内引擎的全部内存状态。"""
    get_engine.cache_clear()
