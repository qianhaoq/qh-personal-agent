package dev.qh.agent

import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderStatusTest {
    @Test fun confirmAlarmRequiresExactSuggestedStatus() {
        assertEquals(true, reminderCanConfirmAlarm("suggested"))
        listOf(null, "", " ", "\t\r\n", "scheduled", "done", "cancelled", "unknown",
            "SUGGESTED", "Suggested", " suggested", "suggested ").forEach {
            assertEquals("status=$it", false, reminderCanConfirmAlarm(it))
        }
    }

    @Test fun snoozeRequiresAnActionableStatus() {
        listOf("suggested", "scheduled").forEach {
            assertEquals(true, reminderCanSnooze(it))
        }
        listOf(null, "", " ", "done", "cancelled", "snoozed", "unknown",
            "SCHEDULED", " scheduled ").forEach {
            assertEquals(false, reminderCanSnooze(it))
        }
    }

    // Corrected: scheduled is server confirmation, not a local AlarmManager success receipt.
    @Test fun knownStatusesUseChineseLabels() {
        mapOf(
            "suggested" to "待确认", "scheduled" to "服务端已确认",
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
