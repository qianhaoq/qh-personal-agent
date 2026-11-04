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
            reminder("ended-uppercase", "DONE"), reminder("ended-padded", " cancelled "),
            nullStatusReminder())
        assertEquals(listOf("suggested", "scheduled"),
            filterRemindersForList(all, ReminderListFilter.ACTIVE).map { it.id })
        assertEquals(all, filterRemindersForList(all, ReminderListFilter.ALL))
        assertEquals(listOf("done", "cancelled"),
            filterRemindersForList(all, ReminderListFilter.ENDED).map { it.id })
        assertEquals(12, all.size)
        assertEquals(emptyList<ReminderDto>(), filterRemindersForList(emptyList(), ReminderListFilter.ACTIVE))
        assertEquals(emptyList<ReminderDto>(), filterRemindersForList(emptyList(), ReminderListFilter.ALL))
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
            filterRemindersForList(all, ReminderListFilter.ALL).map { it.id })
        assertEquals(listOf("early", "same-time", "late", "no-time"),
            filterRemindersForList(all, ReminderListFilter.ACTIVE).map { it.id })
        assertEquals(listOf("done", "cancelled"),
            filterRemindersForList(all, ReminderListFilter.ENDED).map { it.id })
        assertEquals(all, filterRemindersForList(all, ReminderListFilter.ALL))
    }

    @Test fun emptyMessageForNoReminders() {
        ReminderListFilter.entries.forEach { filter ->
            assertEquals(emptyList<ReminderDto>(), filterRemindersForList(emptyList(), filter))
            assertEquals("暂无提醒。", reminderListEmptyMessage(emptyList(), filter))
        }
    }

    @Test fun emptyActiveMessageExplainsHiddenEndedReminders() {
        listOf("done", "cancelled").forEach { status ->
            val all = listOf(reminder(status, status), reminder("unknown", "unknown"))
            assertEquals("暂无待办提醒。可切换到「已结束」或「全部」查看。",
                reminderListEmptyMessage(all, ReminderListFilter.ACTIVE))
        }
    }

    @Test fun emptyEndedMessageExplainsHiddenActiveReminders() {
        listOf("suggested", "scheduled").forEach { status ->
            val all = listOf(reminder(status, status), reminder("unknown", "unknown"))
            assertEquals("暂无已结束提醒。可切换到「待办」或「全部」查看。",
                reminderListEmptyMessage(all, ReminderListFilter.ENDED))
        }
    }

    @Test fun emptyMessageGuidesUnknownStatusesAndFallsBackForVisibleBuckets() {
        val unknown = listOf(reminder("unknown", "unknown"), reminder("padded", " done "),
            reminder("uppercase", "SCHEDULED"), nullStatusReminder())
        assertEquals("暂无待办提醒。可切换到「全部」查看。",
            reminderListEmptyMessage(unknown, ReminderListFilter.ACTIVE))
        assertEquals("暂无已结束提醒。可切换到「全部」查看。",
            reminderListEmptyMessage(unknown, ReminderListFilter.ENDED))
        assertEquals("暂无提醒。", reminderListEmptyMessage(unknown, ReminderListFilter.ALL))
        val mixed = unknown + reminder("active", "scheduled") + reminder("ended", "done")
        ReminderListFilter.entries.forEach { filter ->
            assertEquals("暂无提醒。", reminderListEmptyMessage(mixed, filter))
            assertEquals("暂无提醒。",
                reminderListEmptyMessage(filterRemindersForList(mixed, filter), filter))
        }
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
