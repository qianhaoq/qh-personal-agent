"""v1 个人策略（对标 dsh Policy 的 accept mode）。

accept mode 语义（对个人 agent 的读法，mapping §2）：
- off：不接任何单（引擎只记 Trigger，不开 WorkItem）；
- observe（默认）：被动整理照常（organize_note 无外发副作用），
  主动动作（静默建系统闹钟 / 外发）只记 would-accept，不执行；
- auto：主动动作也放行（v1 未启用）。

Mandate 的 scope 因此分两档：被动面（write_note_derived /
suggest_reminder——「建议」不落闹钟）任何模式都可授；主动面
（schedule_reminder / send_outbound）仅 auto 授。版本化：v1。
"""
from dataclasses import dataclass

from app.domain.model import (
    TRIGGER_ASK_REQUESTED,
    TRIGGER_NOTE_UPLOADED,
    Decision,
    Mandate,
    Trigger,
)

ACCEPT_MODE_OFF = "off"
ACCEPT_MODE_OBSERVE = "observe"
ACCEPT_MODE_AUTO = "auto"

# 被动动作面：只写自己的笔记派生物、只「建议」提醒（不落闹钟/不外发）
PASSIVE_SCOPE = ("write_note_derived", "suggest_reminder")
# 主动动作面：静默改闹钟 / 对外发送——observe 下永不授权
PROACTIVE_SCOPE = ("schedule_reminder", "send_outbound")


@dataclass(frozen=True)
class PersonalPolicy:
    """单用户准入策略（版本化快照，Mandate 引用其 version）。"""

    version: str = "v1"
    accept_mode: str = ACCEPT_MODE_OBSERVE
    # 自动整理开关：关闭后笔记上传也不开 organize WorkItem
    auto_organize: bool = True

    def __post_init__(self):
        if self.accept_mode not in (ACCEPT_MODE_OFF, ACCEPT_MODE_OBSERVE, ACCEPT_MODE_AUTO):
            raise ValueError("invalid accept mode")

    def evaluate(self, trigger: Trigger) -> Decision:
        """Eval 快路径（确定性规则，无 LLM）：判定是否开单。

        note_uploaded 授权被动整理，ask_requested 授权显式问答；
        observe 下均放行。其余类型一律 ignore。
        """
        if self.accept_mode == ACCEPT_MODE_OFF:
            return Decision(
                outcome="ignore",
                reason="policy accept_mode=off",
                strength="inferred",
                would_accept=False,
            )
        if trigger.kind == TRIGGER_ASK_REQUESTED:
            return Decision("accept", "explicit memory question", "explicit", True)
        if trigger.kind == TRIGGER_NOTE_UPLOADED:
            if self.auto_organize:
                return Decision(
                    outcome="accept",
                    reason="笔记上传 → 整理（被动，无外发副作用）",
                    strength="inferred",
                    would_accept=True,
                )
            return Decision(
                outcome="ignore",
                reason="auto_organize 关闭",
                strength="inferred",
                would_accept=False,
            )
        # 未实现的触发不授 Ticket；auto 模式仅记录 would_accept。
        return Decision(
            outcome="ignore",
            reason="unsupported trigger kind",
            strength="inferred",
            would_accept=self.accept_mode == ACCEPT_MODE_AUTO,
        )

    def mandate(self, trigger: Trigger) -> Mandate:
        """accept 后签发的授权快照：scope 按 accept mode 分档。"""
        if trigger.kind == TRIGGER_ASK_REQUESTED:
            return Mandate(self.version, ("search_memory", "answer_ask"))
        scope = PASSIVE_SCOPE
        if self.accept_mode == ACCEPT_MODE_AUTO:
            scope = PASSIVE_SCOPE + PROACTIVE_SCOPE
        return Mandate(policy_version=self.version, scope=scope)
