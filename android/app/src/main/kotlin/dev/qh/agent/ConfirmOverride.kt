package dev.qh.agent

import java.time.Instant
import java.time.format.DateTimeParseException

data class ConfirmOverrides(val fireAt: String?, val title: String?)

/** Blank edits preserve the current value; validate the effective time even without an override. */
fun confirmOverrides(originalTitle: String, originalFireAt: String?, title: String, fireAt: String): ConfirmOverrides {
    require('\u0000' !in title) { "标题不能包含 NUL 字符" }
    val editedTitle = title.trim()
    require(editedTitle.codePointCount(0, editedTitle.length) <= 256) { "标题不能超过 256 字" }
    require('\u0000' !in fireAt) { "提醒时间格式无效" }
    val editedTime = fireAt.trim()
    val effectiveTime = editedTime.ifBlank { originalFireAt.orEmpty().trim() }
    require(effectiveTime.isNotBlank()) { "请填写提醒时间（ISO 格式）" }
    val instant = try { Instant.parse(effectiveTime) }
    catch (_: DateTimeParseException) { throw IllegalArgumentException("提醒时间格式无效，请使用 ISO 时间，例如 2026-11-04T12:00:00Z") }
    return ConfirmOverrides(
        if (editedTime.isBlank() || editedTime == originalFireAt?.trim()) null else instant.toString(),
        editedTitle.takeIf { it.isNotEmpty() && it != originalTitle.trim() }
    )
}
