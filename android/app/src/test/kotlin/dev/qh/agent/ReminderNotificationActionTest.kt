package dev.qh.agent

import dev.qh.data.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class ReminderNotificationActionTest {
    private val reminder = ReminderDto("target", null, "提醒", null, status = "scheduled", rev = 7)
    private class Fake(private val response: SyncResponse) : Repository by MockRepository() {
        var syncs = 0
        var mutation: Pair<String, Int>? = null
        var completed = false
        var failure: Exception? = null
        var afterSync: () -> Unit = {}
        var afterMutation: () -> Unit = {}
        override suspend fun sync(since: Long, limit: Int): SyncResponse {
            syncs++; afterSync(); return response
        }
        override suspend fun completeReminder(id: String, rev: Int): ReminderCompleteResponse {
            failure?.let { throw it }; mutation = id to rev; completed = true; afterMutation()
            return ReminderCompleteResponse(response.reminders.last())
        }
        override suspend fun cancelReminder(id: String, rev: Int): ReminderCancelResponse {
            failure?.let { throw it }; mutation = id to rev; afterMutation()
            return ReminderCancelResponse(response.reminders.last())
        }
    }

    @Test fun actionsResolveCurrentRevAndOnlyThenRunSuccess() = runBlocking {
        ReminderNotificationAction.entries.forEach { action ->
            val repo = Fake(SyncResponse(1, reminders = listOf(reminder.copy(id = "other", rev = 99), reminder)))
            var successes = 0
            performReminderNotificationAction(action, "target", repo, { true }) {
                assertEquals("target" to 7, repo.mutation); successes++
            }
            assertEquals(1, successes)
            assertEquals(action == ReminderNotificationAction.COMPLETE, repo.completed)
        }
    }

    @Test fun failedOrMissingReminderNeverRunsSuccess() = runBlocking {
        listOf(SyncResponse(1), SyncResponse(1, reminders = listOf(reminder)),
            SyncResponse(1, reminders = listOf(reminder), deleted = SyncDeleted(reminders = listOf("target"))))
            .forEach { response ->
                val repo = Fake(response).apply { failure = IllegalStateException() }
                try {
                    performReminderNotificationAction(ReminderNotificationAction.CANCEL, "target", repo, { true }) {
                        fail("Failure must preserve notification and alarms")
                    }
                    fail("Expected failure")
                } catch (_: IllegalStateException) { }
                assertNull(repo.mutation)
            }
    }

    @Test fun sessionFenceStopsBeforeSyncAndAfterSync() = runBlocking {
        val repo = Fake(SyncResponse(1, reminders = listOf(reminder)))
        var current = false
        performReminderNotificationAction(ReminderNotificationAction.COMPLETE, "target", repo, { current }) { fail() }
        assertEquals(0, repo.syncs)
        current = true
        repo.afterSync = { current = false }
        performReminderNotificationAction(ReminderNotificationAction.COMPLETE, "target", repo, { current }) { fail() }
        assertEquals(1, repo.syncs)
        assertNull(repo.mutation)
    }

    @Test fun changedSessionAfterMutationSuppressesLocalSideEffects() = runBlocking {
        var current = true
        val repo = Fake(SyncResponse(1, reminders = listOf(reminder))).apply {
            afterMutation = { current = false }
        }
        performReminderNotificationAction(ReminderNotificationAction.COMPLETE, "target", repo, { current }) { fail() }
        assertEquals("target" to 7, repo.mutation)
    }

    @Test fun cancellationPropagates() = runBlocking {
        val repo = Fake(SyncResponse(1, reminders = listOf(reminder))).apply { failure = CancellationException() }
        try {
            performReminderNotificationAction(ReminderNotificationAction.COMPLETE, "target", repo, { true }) { fail() }
            fail("Expected cancellation")
        } catch (_: CancellationException) { }
    }

    @Test fun onlyKnownDistinctActionsAreAccepted() {
        assertNull(ReminderNotificationAction.fromIntentAction(null))
        assertNull(ReminderNotificationAction.fromIntentAction("dev.qh.agent.action.REMINDER_ALARM"))
        ReminderNotificationAction.entries.forEach {
            assertEquals(it, ReminderNotificationAction.fromIntentAction(it.intentAction))
        }
        assertEquals(2, ReminderNotificationAction.entries.map { it.intentAction }.toSet().size)
    }
}
