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

    @Test fun notificationSnoozeSuccessRejectsCapturedAlarmGenerationAfterCancellation() {
        for (action in listOf("cancel", "complete")) {
            val session = AppRepositories.sessionGeneration()
            val id = "same-reminder-$action"
            val alarms = AppRepositories.alarmGeneration(id)
            val effects = mutableListOf<String>()
            val delayedSnoozeSuccess = {
                AppRepositories.applyNotificationActionLocalEffects(
                    session, id, alarms, signedIn = { true },
                    cancelAlarm = { effects += "cancel alarm" },
                    reinstallAlarm = { effects += "reinstall alarm" },
                    dismissNotification = { effects += "dismiss notification" }
                )
            }
            AppRepositories.invalidateAlarms(id) { effects += action }
            delayedSnoozeSuccess()
            assertEquals(listOf(action), effects)
            assertEquals(session, AppRepositories.sessionGeneration())
        }
    }

    @Test fun sameReminderTerminalSuccessCleansUpAfterSnoozeInvalidatesCapturedGeneration() {
        for (action in listOf("complete", "cancel")) {
            val session = AppRepositories.sessionGeneration()
            val id = "s31-$action"
            val captured = AppRepositories.alarmGeneration(id)
            val effects = mutableListOf<String>()
            var signals = 0
            val unsubscribe = AppRepositories.subscribeRemindersMutated { signals++ }
            try {
                AppRepositories.applyNotificationActionLocalEffects(
                    session, id, captured, signedIn = { true },
                    cancelAlarm = { effects += "snooze cancel" },
                    reinstallAlarm = { effects += "snooze reinstall" },
                    dismissNotification = { effects += "snooze dismiss" }
                )
                assertNotEquals(captured, AppRepositories.alarmGeneration(id))
                AppRepositories.applyNotificationActionLocalEffects(
                    session, id, captured, signedIn = { true },
                    cancelAlarm = { effects += "$action cancel" }, reinstallAlarm = null,
                    dismissNotification = { effects += "$action dismiss" }
                )
                assertEquals(listOf("snooze cancel", "snooze reinstall", "snooze dismiss",
                    "$action cancel", "$action dismiss"), effects)
                assertEquals(2, signals)
                assertEquals(session, AppRepositories.sessionGeneration())
            } finally { unsubscribe() }
        }
    }

    @Test fun staleTerminalSuccessStillRequiresCurrentSignedInSession() {
        for (loggedOut in listOf(false, true)) {
            val session = AppRepositories.sessionGeneration()
            val id = "s31-session-$loggedOut"
            val captured = AppRepositories.alarmGeneration(id)
            AppRepositories.invalidateAlarms(id) {}
            if (!loggedOut) AppRepositories.bumpSession()
            val before = AppRepositories.alarmGeneration(id)
            var signals = 0
            val unsubscribe = AppRepositories.subscribeRemindersMutated { signals++ }
            try {
                AppRepositories.applyNotificationActionLocalEffects(
                    session, id, captured, signedIn = { !loggedOut },
                    cancelAlarm = { fail("Must not cancel") }, reinstallAlarm = null,
                    dismissNotification = { fail("Must not dismiss") }
                )
                assertEquals(before, AppRepositories.alarmGeneration(id))
                assertEquals(0, signals)
            } finally { unsubscribe() }
        }
    }

    @Test fun differentReminderSuccessesBothApplyLocalEffects() {
        val session = AppRepositories.sessionGeneration()
        val ids = listOf("interleaved-A", "interleaved-B")
        val captured = ids.associateWith { AppRepositories.alarmGeneration(it) }
        val global = AppRepositories.alarmGeneration()
        val effects = mutableListOf<String>()
        var refreshes = 0
        val unsubscribe = AppRepositories.subscribeRemindersMutated { refreshes++ }
        try {
            for (id in ids) {
                AppRepositories.applyNotificationActionLocalEffects(
                    session, id, captured.getValue(id), signedIn = { true },
                    cancelAlarm = { effects += "$id cancel" },
                    reinstallAlarm = { effects += "$id reschedule" },
                    dismissNotification = { effects += "$id dismiss" }
                )
            }
            assertEquals(ids.flatMap { listOf("$it cancel", "$it reschedule", "$it dismiss") }, effects)
            assertEquals(2, refreshes)
            var staleSyncApplied = false
            AppRepositories.consumeIfCurrent(session, global) { staleSyncApplied = true }
            assertFalse(staleSyncApplied)
        } finally { unsubscribe() }
    }

    @Test fun bulkInvalidationRejectsCapturedReminderCallback() {
        val session = AppRepositories.sessionGeneration()
        val id = "bulk-deleted-reminder"
        val alarms = AppRepositories.alarmGeneration(id)
        AppRepositories.invalidateAlarms {}
        var applied = false
        AppRepositories.consumeIfCurrent(session, id, alarms) { applied = true }
        assertFalse(applied)
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
