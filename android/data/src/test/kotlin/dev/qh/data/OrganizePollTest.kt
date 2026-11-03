package dev.qh.data

import org.junit.Assert.*
import org.junit.Test

class OrganizePollTest {
    @Test fun uploadedAndMissingContinue() {
        assertTrue(shouldContinueOrganizePoll("uploaded", 0, 0))
        assertTrue(shouldContinueOrganizePoll(null, 1, 1500))
    }
    @Test fun terminalStatusesStop() {
        for (status in listOf("organized", "failed", "other"))
            assertFalse(shouldContinueOrganizePoll(status, 0, 0))
    }
    @Test fun attemptLimitStopsAtBoundary() {
        assertTrue(shouldContinueOrganizePoll("uploaded", 19, 0))
        assertFalse(shouldContinueOrganizePoll("uploaded", 20, 0))
        assertFalse(shouldContinueOrganizePoll("uploaded", 21, 0))
    }
    @Test fun timeoutStopsAtBoundary() {
        assertTrue(shouldContinueOrganizePoll("uploaded", 1, 29_999))
        assertFalse(shouldContinueOrganizePoll("uploaded", 1, 30_000))
        assertFalse(shouldContinueOrganizePoll("uploaded", 1, 30_001))
    }
    @Test fun policyIsConfigurableAndDelayIsBounded() {
        assertFalse(shouldContinueOrganizePoll("uploaded", 2, 0, maxAttempts = 2))
        assertFalse(shouldContinueOrganizePoll("uploaded", 0, 100, timeoutMs = 100))
        for (attempt in 0..20) assertEquals(1500L, nextOrganizePollDelayMs(attempt))
    }
}
