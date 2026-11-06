package dev.qh.agent

import dev.qh.data.Answer
import dev.qh.data.Citation
import org.junit.Assert.*
import org.junit.Test

class AnswerReplayTest {
    private fun replay(answer: String?, busy: Boolean = false,
                       asr: AsrUiState = AsrUiState.Idle,
                       tts: AnswerTtsState = AnswerTtsState.Idle) =
        answerReplayText(answer, busy, asr, tts)

    @Test fun retainedAnswerCanBePlayedAgainAfterStopOrCompletion() {
        val spoken = mutableListOf<String>()
        val answer = Answer(" 今天浇水。\n明天写周报。 ", listOf(Citation("m1", "n1", "引用正文不应播报")))
        replay(answer.answer)?.let(spoken::add)
        assertNull(replay(answer.answer, tts = AnswerTtsState.Speaking))
        replay(answer.answer)?.let(spoken::add)
        assertEquals(listOf("今天浇水。\n明天写周报。", "今天浇水。\n明天写周报。"), spoken)
        assertEquals(" 今天浇水。\n明天写周报。 ", answer.answer)
    }

    @Test fun missingOrBlankAnswerOffersNothingToPlay() {
        listOf(null, "", " \t\n　").forEach { assertNull(replay(it)) }
    }

    @Test fun requestInProgressCannotReplayOldAnswer() {
        assertNull(replay("旧答案", busy = true))
        assertEquals("新答案", replay("新答案"))
    }

    @Test fun allActiveRecognitionStatesPreventPlayback() {
        listOf(AsrUiState.Permission, AsrUiState.Listening, AsrUiState.Processing).forEach {
            assertNull("Recognition state $it must block playback", replay("答案", asr = it))
        }
        assertEquals("答案", replay("答案", asr = AsrUiState.Idle))
    }

    @Test fun unavailableEngineCannotBeInvoked() {
        val spoken = mutableListOf<String>()
        replay("仍可阅读的答案", tts = AnswerTtsState.Unavailable)?.let(spoken::add)
        assertTrue(spoken.isEmpty())
    }

    @Test fun longReplayPreservesEntireBodyIncludingEmoji() {
        val body = "回答😀。".repeat(1500)
        val text = replay(body)!!
        assertEquals(body, answerSpeechChunks(text, 4000).joinToString(""))
    }
}
