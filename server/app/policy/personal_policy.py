"""v0 个人策略（对标 dsh Policy 的 accept mode）。

accept mode 语义（对个人 agent 的读法，mapping §2）：
- off：不接任何单（引擎只记 Trigger，不开 WorkItem）；
- observe（默认）：被动整理照常（organize_note 无外发副作用），
  主动动作（静默建系统闹钟 / 外发）只记 would-accept，不执行；
- auto：主动动作也放行（v0 未启用）。

Mandate 的 scope 因此分两档：被动面（write_note_derived /
suggest_reminder——「建议」不落闹钟）任何模式都可授；主动面
（schedule_reminder / send_outbound）仅 auto 授。版本化：v0。
"""
from dataclasses import dataclass

from app.domain.model import (
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

    version: str = "v0"
    accept_mode: str = ACCEPT_MODE_OBSERVE
    # 自动整理开关：关闭后笔记上传也不开 organize WorkItem
    auto_organize: bool = True

    def evaluate(self, trigger: Trigger) -> Decision:
        """Eval 快路径（确定性规则，无 LLM）：判定是否开单。

        v0 只有 note_uploaded 一个可 accept 的触发：整理是被动行为
        （写自己的笔记派生物），observe 下也放行。其余类型一律
        ignore 并留 would_accept 痕迹（ask 走现有同步路径，PR-2 再入单）。
        """
        if self.accept_mode == ACCEPT_MODE_OFF:
            return Decision(
                outcome="ignore",
                reason="policy accept_mode=off",
                strength="inferred",
                would_accept=False,
            )
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
        # v0 不开单的触发（如 ask_requested）：observe 语义下记「本会接」
        return Decision(
            outcome="ignore",
            reason=f"触发类型 {trigger.kind} 在 v0 不开单",
            strength="inferred",
            would_accept=self.accept_mode == ACCEPT_MODE_AUTO,
        )

    def mandate(self, trigger: Trigger) -> Mandate:
        """accept 后签发的授权快照：scope 按 accept mode 分档。"""
        scope = PASSIVE_SCOPE
        if self.accept_mode == ACCEPT_MODE_AUTO:
            scope = PASSIVE_SCOPE + PROACTIVE_SCOPE
        return Mandate(policy_version=self.version, scope=scope)
