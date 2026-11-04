"""领域模型最小集（对标 dsh 的 Trigger/Thread/Eval/Mandate/Ticket）。

概念对照（docs/superagent-concept-mapping.md §2）：
- Trigger：不可变入口事件，幂等/审计单元（dedupe_key 去重）；
- Thread：时间有序的事件串（个人场景取「一条笔记的整理+确认」粒度）；
- Decision：Eval 快路径（规则）的产出，accept / ignore；
- Mandate：授权快照——只授权被动整理与「建议」提醒，observe 模式下
  永不含静默改闹钟/外发（mapping 的「少打扰」哲学）；
- WorkItem：dsh 的 Ticket 在本仓的对应物，可观测/可停止/可恢复的
  授权工作单元（organize_note / answer_ask / …）。

dataclass 是事务外快照；权威状态持久化于 SQLAlchemy Store。
"""
from dataclasses import dataclass, field
from datetime import datetime

# Trigger kind（入口事件类型）
TRIGGER_NOTE_UPLOADED = "note_uploaded"
TRIGGER_ASK_REQUESTED = "ask_requested"
TRIGGER_USER_CONFIRMED_REMINDER = "user_confirmed_reminder"
TRIGGER_USER_CANCELLED_REMINDER = "user_cancelled_reminder"
TRIGGER_USER_COMPLETED_REMINDER = "user_completed_reminder"
TRIGGER_USER_SNOOZED_REMINDER = "user_snoozed_reminder"

# WorkItem kind（授权工作单元类型）
WORK_ITEM_ORGANIZE_NOTE = "organize_note"
WORK_ITEM_ANSWER_ASK = "answer_ask"

# Subscription kind（Thread 上后续事件直通原 Ticket，不经 Eval）
SUBSCRIPTION_REMINDER_CONFIRM = "reminder_confirm"
SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_STOPPED = "stopped"

# Channel action types（端侧可消费；服务端从不执行 OS 闹钟）
CHANNEL_ACTION_SCHEDULE_REMINDER = "schedule_reminder"
CHANNEL_ACTION_CANCEL_REMINDER = "cancel_reminder"
CHANNEL_ACTION_COMPLETE_REMINDER = "complete_reminder"

# WorkItem 状态机：pending → running → done | failed
WORK_ITEM_PENDING = "pending"
WORK_ITEM_RUNNING = "running"
WORK_ITEM_DONE = "done"
WORK_ITEM_FAILED = "failed"


@dataclass(frozen=True)
class Trigger:
    """不可变入口事件（幂等键 = dedupe_key）。"""

    id: str
    kind: str
    source: str  # 产出该事件的 Source kind（http_api / voice_ingress）
    dedupe_key: str
    user_id: str
    payload: dict
    occurred_at: datetime
    thread_key: str


@dataclass
class Thread:
    """时间有序事件串：同一 thread 上的评估天然串行（Store 事务内串行）。"""

    key: str
    trigger_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """Eval 快路径（规则，无 LLM）的判定。would_accept 用于 observe：
    「本会接单」但被 Policy 拦下时留痕，不真正 accept。"""

    outcome: str  # accept | ignore
    reason: str
    strength: str  # explicit（用户显式要求）| inferred（旁白式被动整理）
    would_accept: bool


@dataclass(frozen=True)
class Mandate:
    """授权快照：绑定 Policy 版本，scope 是允许的动作面。"""

    policy_version: str
    scope: tuple[str, ...]


@dataclass
class WorkItem:
    """授权工作单元（dsh 的 Ticket）：状态自有，不跟执行方走。"""

    id: str
    kind: str
    thread_key: str
    trigger_dedupe_key: str
    user_id: str
    mandate: Mandate
    decision: Decision
    status: str
    created_at: datetime
    updated_at: datetime
    note_id: str | None = None
    note_rev: int | None = None
    worker_id: str | None = None
    result: dict | None = None
    error: str | None = None
    payload: dict = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    locked_at: datetime | None = None


@dataclass(frozen=True)
class DomainEvent:
    """领域事件（可观测性）：engine 唯一写入口产生。"""

    seq: int
    type: str
    at: datetime
    work_item_id: str | None
    detail: str


@dataclass(frozen=True)
class Receipt:
    """engine.receive 的回执：duplicate=True 表示幂等重放（无新副作用）。"""

    trigger_id: str
    duplicate: bool
    decision: Decision | None
    work_item: WorkItem | None
