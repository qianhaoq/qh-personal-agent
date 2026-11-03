package dev.qh.agent

import dev.qh.data.ReminderDto
import org.junit.Assert.*
import org.junit.Test

class ReminderNoteLinkTest {
    @Test fun missingAndBlankIdsCannotOpen() {
        listOf(null, "", " ", "\t\n ").forEach { assertNull(reminderNoteIdForOpen(it)) }
    }

    @Test fun openIdIsTrimmed() {
        assertEquals("n1", reminderNoteIdForOpen(" \tn1\n"))
        assertEquals("n1", reminderNoteIdForOpen("n1"))
    }

    @Test fun relatedRemindersUseExactIdAndPreserveOrderAndStatuses() {
        val first = reminder("r1", "n1", "suggested")
        val second = reminder("r2", "n1", "scheduled")
        val third = reminder("r3", "n1", "done")
        val all = listOf(first, reminder("other", "n2"), reminder("none", null),
            reminder("padded", " n1 "), second, third)
        assertEquals(listOf(first, second, third), remindersForNote("n1", all))
        assertEquals(6, all.size)
    }

    @Test fun noMatchesAndEmptyListReturnEmpty() {
        assertTrue(remindersForNote("n1", listOf(reminder("r1", "n2"), reminder("r2", null))).isEmpty())
        assertTrue(remindersForNote("n1", emptyList()).isEmpty())
    }

    private fun reminder(id: String, noteId: String?, status: String = "suggested") =
        ReminderDto(id, noteId, "提醒", null, status = status, rev = 1)
}
