package dev.qh.agent

import dev.qh.data.readableError
import org.junit.Assert.*
import org.junit.Test

class ConfirmOverrideTest {
    private val time = "2026-11-04T12:00:00Z"
    private fun edits(title: String = "原标题", fireAt: String = time) =
        confirmOverrides("原标题", time, title, fireAt)
    @Test fun unchangedAndBlank() {
        assertEquals(ConfirmOverrides(null, null), edits())
        assertEquals(ConfirmOverrides(null, null), edits(" 原标题 ", " $time "))
        assertEquals(ConfirmOverrides(null, null), edits(" \n", " \n"))
    }
    @Test fun titleOnly() { assertEquals(ConfirmOverrides(null, "新标题"), edits(" 新标题 ")) }
    @Test fun timeOnly() {
        assertEquals(ConfirmOverrides("2026-11-05T12:00:00Z", null), edits(fireAt = "2026-11-05T20:00:00+08:00"))
    }
    @Test fun both() {
        assertEquals(ConfirmOverrides("2026-11-05T12:00:00Z", "新标题"), edits("新标题", "2026-11-05T12:00:00Z"))
    }
    @Test fun missingTime() {
        assertEquals(ConfirmOverrides(time, null), confirmOverrides("原标题", null, "", time))
        invalid("请填写提醒时间") { confirmOverrides("原标题", null, "", " ") }
    }
    @Test fun invalidTime() {
        for (value in listOf("tomorrow", "2026-11-04T12:00:00", "\uD800", "\u0000")) {
            invalid("提醒时间格式无效") { edits(fireAt = value) }
        }
        invalid("提醒时间格式无效") { confirmOverrides("原标题", "invalid", "", "") }
    }
    @Test fun titleLength() {
        invalid("标题不能超过 256 字") { edits("字".repeat(257)) }
        val boundary = "😀".repeat(256)
        assertEquals(boundary, edits(boundary).title)
    }
    @Test fun nulTitle() {
        invalid("标题不能包含 NUL") { edits("标题\u0000") }
        invalid("标题不能包含 NUL") { edits("\u0000原标题") }
    }
    private fun invalid(expected: String, block: () -> Unit) {
        try { block(); fail("Expected validation failure") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e), readableError(e).contains(expected)) }
    }
}
