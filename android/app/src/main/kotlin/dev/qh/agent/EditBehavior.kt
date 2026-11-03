package dev.qh.agent

import dev.qh.data.Note

fun shouldPollOrganizeAfterEdit(status: String) = status == "uploaded"
fun shouldPopEditRoute(route: String?) = route == "edit"

fun syncDetailNote(detail: Note?, notes: List<Note>): Note? =
    detail?.let { current -> notes.find { it.id == current.id } ?: current }
