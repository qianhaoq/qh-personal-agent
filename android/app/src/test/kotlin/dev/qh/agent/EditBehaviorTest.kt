package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class EditBehaviorTest {
    @Test fun uploadedPollsEvenWhenRevisionAndStatusDidNotChange() {
        assertTrue(shouldPollOrganizeAfterEdit("uploaded"))
        assertFalse(shouldPollOrganizeAfterEdit("organized"))
        assertFalse(shouldPollOrganizeAfterEdit("failed"))
    }

    @Test fun saveCompletionOnlyPopsEdit() {
        assertTrue(shouldPopEditRoute("edit"))
        assertFalse(shouldPopEditRoute("notes"))
        assertFalse(shouldPopEditRoute(null))
    }
}
