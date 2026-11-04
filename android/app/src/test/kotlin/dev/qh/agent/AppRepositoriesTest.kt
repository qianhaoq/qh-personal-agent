package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

class AppRepositoriesTest {
    @Test fun mutationSignalCarriesCurrentGenerationAndUnsubscribes() {
        val received = mutableListOf<Long>()
        val unsubscribe = AppRepositories.subscribeRemindersMutated { received += it }
        try {
            AppRepositories.notifyRemindersMutated()
            assertEquals(listOf(AppRepositories.sessionGeneration()), received)
            unsubscribe()
            AppRepositories.notifyRemindersMutated()
            assertEquals(1, received.size)
        } finally { unsubscribe() }
    }

    @Test fun oldSessionHandlerCannotRefreshAfterSessionBump() {
        val subscribedSession = AppRepositories.sessionGeneration()
        var refreshes = 0
        val unsubscribe = AppRepositories.subscribeRemindersMutated { eventSession ->
            if (eventSession == subscribedSession) {
                AppRepositories.ifCurrent(eventSession) { refreshes++ }
            }
        }
        try {
            AppRepositories.notifyRemindersMutated()
            assertEquals(1, refreshes)
            AppRepositories.bumpSession()
            AppRepositories.notifyRemindersMutated()
            assertEquals(1, refreshes)
        } finally { unsubscribe() }
    }

    @Test fun queuedMutationCannotRefreshAfterSessionBump() {
        val queued = mutableListOf<() -> Unit>()
        var refreshes = 0
        val unsubscribe = AppRepositories.subscribeRemindersMutated { eventSession ->
            queued += { AppRepositories.ifCurrent(eventSession) { refreshes++ } }
        }
        try {
            AppRepositories.notifyRemindersMutated()
            assertEquals(1, queued.size)
            AppRepositories.bumpSession()
            queued.single().invoke()
            assertEquals(0, refreshes)
        } finally { unsubscribe() }
    }

    @Test fun mutationWithoutSubscribersIsSafe() {
        AppRepositories.notifyRemindersMutated()
    }

    @Test fun deleteInvalidatesOldSyncWithoutInvalidatingOrganizeSession() {
        val session = AppRepositories.sessionGeneration()
        val alarms = AppRepositories.alarmGeneration()
        val effects = mutableListOf<String>()
        AppRepositories.invalidateAlarms { effects += "cancel" }
        AppRepositories.consumeIfCurrent(session, alarms) { effects += "stale" }
        AppRepositories.consumeIfCurrent(session, AppRepositories.alarmGeneration()) { effects += "fresh" }
        assertEquals(listOf("cancel", "fresh"), effects)
        assertEquals(session, AppRepositories.sessionGeneration())
    }

    @Test fun delayedBootResponseCannotConsumeDuringOrAfterDeleteCancellation() {
        val session = AppRepositories.sessionGeneration()
        val alarms = AppRepositories.alarmGeneration()
        val ready = CountDownLatch(1)
        val finished = CountDownLatch(1)
        var consumed = false
        lateinit var boot: Thread
        AppRepositories.invalidateAlarms {
            boot = thread {
                ready.countDown()
                AppRepositories.consumeIfCurrent(session, alarms) { consumed = true }
                finished.countDown()
            }
            assertTrue(ready.await(5, TimeUnit.SECONDS))
            assertFalse(finished.await(100, TimeUnit.MILLISECONDS))
        }
        assertTrue(finished.await(5, TimeUnit.SECONDS))
        boot.join()
        assertFalse(consumed)
    }
}
