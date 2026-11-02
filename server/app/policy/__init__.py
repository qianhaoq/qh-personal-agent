"""准入策略包：policy/* 是唯一准入与主动强度来源（映射边界规则 3）。"""
from app.policy.personal_policy import PersonalPolicy

__all__ = ["PersonalPolicy"]
