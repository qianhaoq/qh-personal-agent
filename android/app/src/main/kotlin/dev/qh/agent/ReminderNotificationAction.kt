package dev.qh.agent

import dev.qh.data.Repository
import dev.qh.data.ReminderSnoozeResponse

internal enum class ReminderNotificationAction(val intentAction: String, val label: String) {
    COMPLETE("dev.qh.agent.action.REMINDER_COMPLETE", "完成"),
    CANCEL("dev.qh.agent.action.REMINDER_CANCEL", "取消"),
    SNOOZE("dev.qh.agent.action.REMINDER_SNOOZE", "贪睡10分钟");

    companion object {
        fun fromIntentAction(value: String?) = entries.find { it.intentAction == value }
    }
}

/** Resolve rev from a fresh full sync; never clear alarms/notification on a failed mutation. */
internal suspend fun performReminderNotificationAction(
    action: ReminderNotificationAction, id: String, repository: Repository,
    sessionCurrent: () -> Boolean, onSuccess: (ReminderSnoozeResponse?) -> Unit
) {
    if (!sessionCurrent()) return
    val response = repository.sync()
    if (!sessionCurrent()) return
    val reminder = response.reminders.find { it.id == id && it.id !in response.deleted.reminders }
        ?: error("Reminder unavailable")
    val snoozed = when (action) {
        ReminderNotificationAction.COMPLETE -> { repository.completeReminder(id, reminder.rev); null }
        ReminderNotificationAction.CANCEL -> { repository.cancelReminder(id, reminder.rev); null }
        ReminderNotificationAction.SNOOZE -> repository.snoozeReminder(id, reminder.rev, 10)
    }
    if (sessionCurrent()) onSuccess(snoozed)
}
