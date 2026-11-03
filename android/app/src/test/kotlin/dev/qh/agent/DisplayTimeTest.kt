package dev.qh.agent

import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Test

class DisplayTimeTest {
    private val shanghai = ZoneId.of("Asia/Shanghai")

    @Test fun nullAndBlankAreEmpty() {
        listOf(null, "", " ", "\t\r\n", "\u2003\u00a0").forEach {
            assertEquals("", formatDisplayTime(it, shanghai))
            assertEquals("未设置", formatDisplayTimeOrUnset(it, shanghai))
        }
    }

    @Test fun zuluUsesInjectedZoneAndOmitsSeconds() {
        assertEquals("2026年11月3日 20:00", formatDisplayTime("2026-11-03T12:00:59Z", shanghai))
        assertEquals("2026年11月3日 12:00", formatDisplayTime("2026-11-03T12:00:00Z", ZoneId.of("UTC")))
    }

    @Test fun offsetsRepresentInstantsAndCanCrossDayBoundaries() {
        assertEquals("2026年11月3日 20:00", formatDisplayTime("2026-11-03T14:00:00+02:00", shanghai))
        assertEquals("2026年11月4日 03:30", formatDisplayTime("2026-11-03T14:30:00-05:00", shanghai))
        assertEquals("2026年11月3日 20:00", formatDisplayTime("2026-11-03T14:00+02:00", shanghai))
    }

    @Test fun dateOnlyKeepsCalendarDate() {
        assertEquals("2026年11月3日", formatDisplayTime("2026-11-03", shanghai))
        assertEquals("2026年11月3日", formatDisplayTime("2026-11-03", ZoneId.of("America/New_York")))
    }

    @Test fun invalidValuesFallBackToTrimmedInput() {
        listOf("+1000000000-12-31T23:59:59Z", "not a date", "2026-02-30", "2026-11-03T25:00Z", "\uD800", "x\uDC00", "\u200B").forEach {
            assertEquals(it, formatDisplayTime(" \t$it\n", shanghai))
        }
    }

    @Test fun unusualSurroundingWhitespaceIsTrimmed() {
        assertEquals("2026年11月3日 20:00", formatDisplayTime("\u2003\u00a02026-11-03T12:00:00Z\u2003", shanghai))
        assertEquals("2026年11月3日 20:00", formatDisplayTimeOrUnset("2026-11-03T12:00:00Z", shanghai))
    }
}
