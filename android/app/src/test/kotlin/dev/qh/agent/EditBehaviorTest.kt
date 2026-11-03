package dev.qh.agent

import dev.qh.data.Note
import dev.qh.data.NoteTodo
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
    @Test fun detailSyncPropagatesOrganizedFieldsAndRevision() {
        val detail = Note("n1", "正文", "uploaded", 2)
        val organized = detail.copy(status = "organized", rev = 3, title = "标题",
            tags = listOf("标签"), todos = listOf(NoteTodo("待办")), category = "生活")
        assertEquals(organized, syncDetailNote(detail, listOf(Note("n2", "其他", "uploaded", 1), organized)))
    }

    @Test fun detailSyncKeepsDetailWhenMissingFromList() {
        val detail = Note("n1", "正文", "uploaded", 2)
        assertSame(detail, syncDetailNote(detail, listOf(Note("n2", "其他", "organized", 3))))
        assertSame(detail, syncDetailNote(detail, emptyList()))
    }

    @Test fun detailSyncKeepsNullDetail() {
        assertNull(syncDetailNote(null, listOf(Note("n1", "正文", "organized", 3))))
    }
}
