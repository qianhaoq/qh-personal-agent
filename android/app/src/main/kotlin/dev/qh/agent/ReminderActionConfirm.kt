package dev.qh.agent

enum class ReminderAction { COMPLETE, CANCEL }

fun reminderActionConfirmTitle(action: ReminderAction): String = when (action) {
    ReminderAction.COMPLETE -> "完成提醒？"
    ReminderAction.CANCEL -> "取消提醒？"
}

fun reminderConfirmDisplayTitle(raw: String?): String = raw.orEmpty().ifBlank { "随记提醒" }

fun reminderNotificationContentTitle(raw: String?): String = reminderConfirmDisplayTitle(raw)

fun reminderNotificationContentText(): String = "随记提醒"

fun reminderActionConfirmMessage(action: ReminderAction, reminderTitle: String): String = when (action) {
    ReminderAction.COMPLETE -> "确认后将标记「${reminderConfirmDisplayTitle(reminderTitle)}」为已完成。"
    ReminderAction.CANCEL -> "确认后将取消提醒「${reminderConfirmDisplayTitle(reminderTitle)}」。"
}

fun shouldAcceptReminderActionConfirm(busy: Boolean): Boolean = !busy
