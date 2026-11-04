package dev.qh.agent

import dev.qh.data.ReminderDto

/** Only exact active statuses belong in the default main list. */
fun reminderIsActive(status: String?): Boolean = status == "suggested" || status == "scheduled"

/** Presentation only: retain sync order and keep the source list available to note detail. */
fun filterRemindersForList(reminders: List<ReminderDto>, showEnded: Boolean): List<ReminderDto> =
    if (showEnded) reminders else reminders.filter { reminderIsActive(it.status) }

/** Explain when the default list is empty because non-active reminders are hidden. */
fun reminderListEmptyMessage(allReminders: List<ReminderDto>, showEnded: Boolean): String =
    when {
        allReminders.isEmpty() -> "暂无提醒。"
        !showEnded && allReminders.any { !reminderIsActive(it.status) } &&
            filterRemindersForList(allReminders, showEnded).isEmpty() ->
            "暂无进行中提醒。可开启「显示已结束」查看。"
        else -> "暂无提醒。"
    }

/** Display only: preserve the source status for requests and reminder actions. */
fun reminderStatus(status: String?): String = when (val value = status?.trim().orEmpty()) {
    "suggested" -> "待确认"
    "scheduled" -> "已排程"
    "done" -> "已完成"
    "cancelled" -> "已取消"
    "" -> "未知"
    else -> value
}

/** Match the actionable states accepted by both Mock and Real snooze requests. */
fun reminderCanSnooze(status: String?): Boolean = status == "suggested" || status == "scheduled"
