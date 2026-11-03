package dev.qh.agent

import dev.qh.data.Note

fun shouldPollOrganizeAfterEdit(status: String) = status == "uploaded"
fun shouldPopEditRoute(route: String?) = route == "edit"

/** Align detail snapshot with a freshly published notes list; clear if the note vanished. */
fun syncedDetailNote(detail: Note?, notes: List<Note>): Note? =
    detail?.let { current -> notes.find { it.id == current.id } }
