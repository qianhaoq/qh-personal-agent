package dev.qh.agent

import dev.qh.data.ReminderDto

/** Only exact active statuses belong in the default main list. */
fun reminderIsActive(status: String?): Boolean = status == "suggested" || status == "scheduled"

enum class ReminderListFilter(val label: String) {
    ACTIVE("待办"), ENDED("已结束"), ALL("全部")
}

private fun reminderIsEnded(status: String?): Boolean = status == "done" || status == "cancelled"

/** Presentation only: retain sync order and keep the source list available to note detail. */
fun filterRemindersForList(reminders: List<ReminderDto>, listFilter: ReminderListFilter): List<ReminderDto> =
    when (listFilter) {
        ReminderListFilter.ACTIVE -> reminders.filter { reminderIsActive(it.status) }
        ReminderListFilter.ENDED -> reminders.filter { reminderIsEnded(it.status) }
        ReminderListFilter.ALL -> reminders
    }

/** Explain where hidden reminders can be viewed when the selected bucket is empty. */
fun reminderListEmptyMessage(allReminders: List<ReminderDto>, listFilter: ReminderListFilter): String =
    when {
        filterRemindersForList(allReminders, listFilter).isNotEmpty() -> "暂无提醒。"
        listFilter == ReminderListFilter.ACTIVE && allReminders.any { reminderIsEnded(it.status) } ->
            "暂无待办提醒。可切换到「已结束」或「全部」查看。"
        listFilter == ReminderListFilter.ENDED && allReminders.any { reminderIsActive(it.status) } ->
            "暂无已结束提醒。可切换到「待办」或「全部」查看。"
        listFilter == ReminderListFilter.ACTIVE && allReminders.isNotEmpty() ->
            "暂无待办提醒。可切换到「全部」查看。"
        listFilter == ReminderListFilter.ENDED && allReminders.isNotEmpty() ->
            "暂无已结束提醒。可切换到「全部」查看。"
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
