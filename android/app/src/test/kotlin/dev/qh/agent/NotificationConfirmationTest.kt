package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class NotificationConfirmationTest {
    @Test fun durableSessionSurvivesProcessRecreationButRejectsNewLoginAndSignOut() {
        val savedEpoch = "persisted-login"
        // Local AtomicLong may restart at zero: it is deliberately absent from this gate.
        assertTrue(confirmationSessionCurrent(savedEpoch, "persisted-login", true))
        assertFalse(confirmationSessionCurrent(savedEpoch, "next-login", true))
        assertFalse(confirmationSessionCurrent(savedEpoch, savedEpoch, false))
    }

    @Test fun waitsForCompletionAndBlocksDismissAndDoubleSubmit() {
        val state = NotificationConfirmation()
        var finishes = 0
        var calls = 0
        lateinit var done: () -> Unit
        state.attach(Any()) { finishes++ }
        assertTrue(state.submit(true) { calls++; done = it })
        assertTrue(state.busy)
        assertFalse(state.canDismiss)
        assertEquals(0, finishes)
        assertFalse(state.submit(true) { calls++ })
        assertEquals(1, calls)
        done()
        assertEquals(1, finishes)
        assertFalse(state.busy)
    }

    @Test fun rotationFinishesOnlyLiveActivityEvenIfCompletionFallsBetweenInstances() {
        val state = NotificationConfirmation()
        val old = Any()
        val live = Any()
        var oldFinishes = 0
        var liveFinishes = 0
        lateinit var done: () -> Unit
        state.attach(old) { oldFinishes++ }
        state.submit(true) { done = it }
        state.detach(old)
        done()
        assertEquals(0, oldFinishes)
        state.attach(live) { liveFinishes++ }
        state.detach(old)
        assertEquals(1, liveFinishes)
        assertFalse(state.submit(true) { fail("must not replay") })
    }

    @Test fun rotationWhileBusyKeepsRequestAndRoutesCompletionToReplacement() {
        val state = NotificationConfirmation()
        val old = Any()
        var finishes = 0
        lateinit var done: () -> Unit
        state.attach(old) { fail("destroyed Activity") }
        state.submit(true) { done = it }
        state.attach(Any()) { finishes++ }
        state.detach(old)
        assertTrue(state.busy)
        assertFalse(state.canDismiss)
        done()
        assertEquals(1, finishes)
    }

    @Test fun invalidSessionNeverSubmits() {
        val state = NotificationConfirmation()
        assertFalse(state.submit(false) { fail("invalid session") })
        assertTrue(state.canDismiss)
        assertFalse(state.submitted)
    }
}
