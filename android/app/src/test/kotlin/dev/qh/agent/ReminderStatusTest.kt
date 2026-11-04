package dev.qh.agent

import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderStatusTest {
    @Test fun knownStatusesUseChineseLabels() {
        mapOf(
            "suggested" to "待确认", "scheduled" to "已排程",
            "done" to "已完成", "cancelled" to "已取消"
        ).forEach { (status, label) ->
            assertEquals(label, reminderStatus(status))
            assertEquals(label, reminderStatus(" \t$status\n"))
        }
    }

    @Test fun unknownStatusesPreserveTrimmedValue() {
        listOf("snoozed", "SCHEDULED", "future status", "自定义状态").forEach {
            assertEquals(it, reminderStatus(" \t$it\n"))
        }
    }

    @Test fun nullAndBlankUseUnknownLabel() {
        listOf(null, "", " ", "\t\r\n", "\u2003\u00a0").forEach {
            assertEquals("未知", reminderStatus(it))
        }
    }
}
