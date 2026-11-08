package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class LocalScheduleResultTest {
    @Test fun onlySuccessfulSystemCallCountsAsSubmitted() {
        assertEquals(LocalScheduleResult.SUBMITTED_EXACT, submitLocalAlarm(false, { true }, {}, { fail() }))
        assertEquals(LocalScheduleResult.SUBMITTED_INEXACT, submitLocalAlarm(false, { false }, { fail() }, {}))
        assertEquals(LocalScheduleResult.SUBMITTED_INEXACT,
            submitLocalAlarm(false, { true }, { throw SecurityException() }, {}))
        assertEquals(LocalScheduleResult.FAILED,
            submitLocalAlarm(false, { true }, { throw SecurityException() }, { throw SecurityException() }))
        assertEquals(LocalScheduleResult.FAILED,
            submitLocalAlarm(false, { true }, { throw IllegalStateException() }, { fail() }))
        assertEquals(LocalScheduleResult.EXPIRED,
            submitLocalAlarm(true, { fail(); true }, { fail() }, { fail() }))
        assertFalse(LocalScheduleResult.UNKNOWN.submitted)
        assertFalse(LocalScheduleResult.FAILED.submitted)
    }
}

class ReminderStepResultTest {
    @Test fun successfulConfirmSurvivesScheduleAndRefreshFailureWithoutRepost() = kotlinx.coroutines.runBlocking {
        var posts = 0
        var applied: Pair<String, LocalScheduleResult>? = null
        val outcome = confirmReminderSteps(
            confirm = { posts++; dev.qh.data.ReminderConfirmResponse(dev.qh.data.ReminderDto(
                "r", "n", "title", "2099-01-01T00:00:00Z", status = "scheduled", rev = 3)) },
            schedule = { throw SecurityException() },
            apply = { reminder, local -> applied = reminder.status to local },
            refresh = { throw java.io.IOException() })
        assertEquals(1, posts)
        assertEquals("scheduled" to LocalScheduleResult.FAILED, applied)
        assertTrue(outcome.refreshFailed)
        assertEquals(LocalScheduleResult.FAILED, outcome.local)
    }
}
