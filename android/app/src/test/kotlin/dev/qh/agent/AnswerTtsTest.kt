package dev.qh.agent

import org.junit.Assert.*
import org.junit.Test

class AnswerTtsTest {
    @Test fun nullDoesNotSpeak() { assertNull(speakTextForAnswer(null)) }
    @Test fun emptyDoesNotSpeak() { assertNull(speakTextForAnswer("")) }
    @Test fun whitespaceDoesNotSpeak() { assertNull(speakTextForAnswer(" \t\r\n　")) }
    @Test fun trimsOnlyEdges() { assertEquals("今天浇水。\n明天 写周报。", speakTextForAnswer(" \n今天浇水。\n明天 写周报。\t")) }
    @Test fun preservesAnswerBody() { assertEquals("今天记了两条笔记。", speakTextForAnswer("今天记了两条笔记。")) }
    @Test fun longAnswerChunksPreserveAllText() {
        val body = "中文😀答案。".repeat(1000)
        val chunks = answerSpeechChunks(body, 4000)
        assertEquals(body, chunks.joinToString(""))
        assertTrue(chunks.all { it.length <= 4000 && !it.last().isHighSurrogate() && !it.first().isLowSurrogate() })
    }
    @Test fun exactLimitFitsOneUtterance() {
        assertEquals(listOf("回答"), answerSpeechChunks("回答", 2))
    }
}
