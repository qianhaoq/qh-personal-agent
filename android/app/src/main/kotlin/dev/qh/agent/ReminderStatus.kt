package dev.qh.agent

/** Display only: preserve the source status for requests and reminder actions. */
fun reminderStatus(status: String?): String = when (val value = status?.trim().orEmpty()) {
    "suggested" -> "待确认"
    "scheduled" -> "已排程"
    "done" -> "已完成"
    "cancelled" -> "已取消"
    "" -> "未知"
    else -> value
}
