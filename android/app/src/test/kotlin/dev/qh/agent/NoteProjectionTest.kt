package dev.qh.agent

import dev.qh.data.Note
import org.junit.Assert.*
import org.junit.Test

class NoteProjectionTest {
    @Test fun oldFullRefreshCannotEraseCreateOrResurrectDelete() {
        val projection = NoteProjection()
        val a = Note("a", "A", "organized", 1)
        val old = projection.begin()
        var current = projection.upsert(a, emptyList())
        assertEquals(current, projection.apply(old, emptyList(), current))
        val beforeDelete = projection.begin()
        current = projection.delete(a.id, current)
        assertTrue(projection.apply(beforeDelete, listOf(a), current).isEmpty())
        assertTrue(projection.apply(projection.begin(), listOf(a), current).isEmpty())
        assertTrue(projection.upsert(a, current).isEmpty())
    }
    @Test fun oldRevisionAndOldRefreshIdCannotOverrideNewProjection() {
        val projection = NoteProjection()
        val fresh = Note("a", "new", "organized", 4)
        val older = fresh.copy(text = "old", rev = 2)
        val stale = projection.begin()
        val latest = projection.begin()
        assertEquals(listOf(fresh), projection.apply(stale, listOf(older), listOf(fresh)))
        assertEquals(listOf(fresh), projection.apply(latest, listOf(older), listOf(fresh)))
    }
}
