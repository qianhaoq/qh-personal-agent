package dev.qh.agent

import dev.qh.data.ReminderDto
import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderListFilterTest {
    @Test fun activeStatusesRequireExactMatch() {
        listOf("suggested", "scheduled").forEach {
            assertEquals(true, reminderIsActive(it))
        }
        listOf(null, "", " ", "\t\n", "done", "cancelled", "unknown", "snoozed",
            "SCHEDULED", "Suggested", " scheduled ", "suggested ").forEach {
            assertEquals(false, reminderIsActive(it))
        }
    }

    @Test fun defaultListHidesEveryNonActiveStatus() {
        val all = listOf(reminder("suggested", "suggested"), reminder("done", "done"),
            reminder("scheduled", "scheduled"), reminder("cancelled", "cancelled"),
            reminder("blank", ""), reminder("space", " \t"), reminder("unknown", "unknown"),
            reminder("uppercase", "SCHEDULED"), reminder("padded", " scheduled "),
            nullStatusReminder())
        assertEquals(listOf("suggested", "scheduled"),
            filterRemindersForList(all, showEnded = false).map { it.id })
        assertEquals(all, filterRemindersForList(all, showEnded = true))
        assertEquals(10, all.size)
        assertEquals(emptyList<ReminderDto>(), filterRemindersForList(emptyList(), false))
        assertEquals(emptyList<ReminderDto>(), filterRemindersForList(emptyList(), true))
    }

    @Test fun filteringPreservesActiveOrderInFullTimeSortedList() {
        val all = listOf(
            reminder("no-time", "scheduled"),
            reminder("late", "suggested", "2026-11-04T12:00:00Z"),
            reminder("done", "done", "2026-11-04T08:00:00Z"),
            reminder("early", "scheduled", "2026-11-04T09:00:00Z"),
            reminder("same-time", "suggested", "2026-11-04T09:00:00Z"),
            reminder("cancelled", "cancelled")
        ).sortedWith(compareBy(nullsLast()) { it.fire_at })
        assertEquals(listOf("done", "early", "same-time", "late", "no-time", "cancelled"),
            filterRemindersForList(all, true).map { it.id })
        assertEquals(listOf("early", "same-time", "late", "no-time"),
            filterRemindersForList(all, false).map { it.id })
        assertEquals(all, filterRemindersForList(all, true))
    }

    // JSON/Gson can materialize a null status despite the DTO's Kotlin type.
    private fun nullStatusReminder(): ReminderDto {
        val reminder = ReminderDto("null", "note", "提醒", null, status = "done", rev = 1)
        ReminderDto::class.java.getDeclaredField("status").apply {
            isAccessible = true
            set(reminder, null)
        }
        return reminder
    }

    private fun reminder(id: String, status: String, fireAt: String? = null) =
        ReminderDto(id, "note", "提醒", fireAt, status = status, rev = 1)
}
