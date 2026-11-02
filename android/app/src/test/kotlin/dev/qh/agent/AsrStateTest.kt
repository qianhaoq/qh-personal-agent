package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class AsrStateTest {
    @Test fun permissionThenListeningAndStopStillAcceptsFinalResult() {
        var state = reduceAsr(AsrUiState.Idle, AsrEvent.RequestPermission)
        assertEquals(AsrUiState.Permission, state)
        assertFalse(state.acceptsResult())
        state = reduceAsr(state, AsrEvent.Granted)
        assertEquals(AsrUiState.Listening, state)
        assertTrue(state.acceptsResult())
        state = reduceAsr(state, AsrEvent.Stop)
        assertEquals(AsrUiState.Processing, state)
        assertTrue(state.acceptsResult())
        assertEquals(AsrUiState.Idle, reduceAsr(state, AsrEvent.Finish))
    }
    @Test fun denialAllowsTextAndRetry() {
        val denied = reduceAsr(AsrUiState.Permission, AsrEvent.Denied)
        assertEquals(AsrUiState.Idle, denied)
        assertEquals(AsrUiState.Listening, reduceAsr(denied, AsrEvent.Start))
    }
    @Test fun cancellationInEveryPhaseRejectsLateResultsAndPermission() {
        for (phase in AsrUiState.entries) {
            val cancelled = reduceAsr(phase, AsrEvent.Cancel)
            assertFalse(cancelled.acceptsResult())
            assertEquals(AsrUiState.Idle, reduceAsr(cancelled, AsrEvent.Granted))
            assertEquals(AsrUiState.Idle, reduceAsr(cancelled, AsrEvent.Stop))
        }
    }
    @Test fun repeatedStartDoesNotReenterActiveAttempt() {
        for (phase in listOf(AsrUiState.Permission, AsrUiState.Listening, AsrUiState.Processing)) {
            assertEquals(phase, reduceAsr(phase, AsrEvent.Start))
            assertEquals(phase, reduceAsr(phase, AsrEvent.RequestPermission))
        }
    }
}
