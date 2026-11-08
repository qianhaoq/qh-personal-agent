package dev.qh.agent

import dev.qh.data.Note

/** Full snapshots and local mutations share a version fence; rev alone cannot fence deletion. */
internal class NoteProjection {
    data class Refresh(val id: Long, val version: Long)
    private var refreshId = 0L
    private var version = 0L
    private val deleted = mutableSetOf<String>()
    @Synchronized fun begin() = Refresh(++refreshId, version)
    @Synchronized fun clear() { refreshId++; version++; deleted.clear() }
    @Synchronized fun delete(id: String, current: List<Note>): List<Note> {
        deleted += id; version++
        return current.filterNot { it.id == id }
    }
    @Synchronized fun upsert(note: Note, current: List<Note>): List<Note> {
        if (note.id in deleted) return current
        version++
        val existing = current.find { it.id == note.id }
        if (existing != null && existing.rev > note.rev) return current
        return listOf(note) + current.filterNot { it.id == note.id }
    }
    @Synchronized fun apply(ticket: Refresh, incoming: List<Note>, current: List<Note>): List<Note> {
        if (ticket.id != refreshId || ticket.version != version) return current
        val previous = current.associateBy { it.id }
        return incoming.filterNot { it.id in deleted }.map { note ->
            previous[note.id]?.takeIf { it.rev > note.rev } ?: note
        }
    }
}
