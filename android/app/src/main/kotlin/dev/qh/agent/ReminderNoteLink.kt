package dev.qh.agent

import dev.qh.data.ReminderDto

fun reminderNoteIdForOpen(noteId: String?): String? = noteId?.trim()?.takeIf { it.isNotEmpty() }

fun remindersForNote(noteId: String, reminders: List<ReminderDto>): List<ReminderDto> =
    reminders.filter { it.note_id == noteId }
