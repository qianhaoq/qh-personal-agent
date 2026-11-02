package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class VoiceQuestionTest {
    @Test fun idleResultFillsQuestionAndAutoAsks() {
        val result = voiceQuestion("昨天记了什么？", busy = false)
        assertEquals("昨天记了什么？", result.question)
        assertTrue(result.shouldAutoAsk)
    }

    @Test fun busyResultStillFillsQuestionForManualRetry() {
        val result = voiceQuestion("绿植什么时候浇水？", busy = true)
        assertEquals("绿植什么时候浇水？", result.question)
        assertFalse(result.shouldAutoAsk)
    }
}
