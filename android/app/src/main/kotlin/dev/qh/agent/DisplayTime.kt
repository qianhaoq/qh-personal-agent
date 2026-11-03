package dev.qh.agent

import java.time.DateTimeException
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.util.Locale

private val displayDateTime = DateTimeFormatter.ofPattern("uuuu年M月d日 HH:mm", Locale.CHINA)
private val displayDate = DateTimeFormatter.ofPattern("uuuu年M月d日", Locale.CHINA)

/** Display only: keep the source ISO value for sorting, requests and alarms. */
fun formatDisplayTime(iso: String?, zone: ZoneId = ZoneId.systemDefault()): String {
    val value = iso?.trim().orEmpty()
    if (value.isEmpty()) return ""
    val instant = try {
        Instant.parse(value)
    } catch (_: DateTimeParseException) {
        try {
            OffsetDateTime.parse(value).toInstant()
        } catch (_: DateTimeParseException) {
            // Date-only due values represent a calendar date, without a time zone.
            return try {
                LocalDate.parse(value).format(displayDate)
            } catch (_: DateTimeParseException) {
                value
            }
        }
    }
    return try {
        displayDateTime.withZone(zone).format(instant)
    } catch (_: DateTimeException) {
        value
    }
}

fun formatDisplayTimeOrUnset(iso: String?, zone: ZoneId = ZoneId.systemDefault()): String =
    formatDisplayTime(iso, zone).ifEmpty { "未设置" }
