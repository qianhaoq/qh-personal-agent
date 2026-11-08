package dev.qh.agent

import dev.qh.data.RequestOwner
import org.junit.Assert.*
import org.junit.Test

class VoiceAttemptTest {
    @Test fun permissionAndProcessingResultsCannotConsumeDifferentDraftOrModeOrOwner() {
        val attempt = VoiceAttempt(RequestOwner("real", "u", "e", "g"), "note", 10)
        assertTrue(attempt.accepts(true, "note", 10))
        assertFalse(attempt.accepts(false, "note", 10))
        assertFalse(attempt.accepts(true, "ask", 10))
        assertFalse(attempt.accepts(true, "note", 11))
    }
}
