package dev.qh.agent

enum class ReminderAction { COMPLETE, CANCEL }

fun reminderActionConfirmTitle(action: ReminderAction): String = when (action) {
    ReminderAction.COMPLETE -> "完成提醒？"
    ReminderAction.CANCEL -> "取消提醒？"
}

fun reminderActionConfirmMessage(action: ReminderAction): String = when (action) {
    ReminderAction.COMPLETE -> "确认后将标记为已完成。"
    ReminderAction.CANCEL -> "确认后将取消该提醒。"
}

fun shouldAcceptReminderActionConfirm(busy: Boolean): Boolean = !busy
