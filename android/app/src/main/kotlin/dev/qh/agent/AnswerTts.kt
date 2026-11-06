package dev.qh.agent

enum class AnswerTtsState { Idle, Speaking, Unavailable }

fun speakTextForAnswer(answer: String?): String? = answer?.trim()?.takeIf { it.isNotEmpty() }

/** Only an explicit tap may replay a retained answer; never compete with recognition. */
internal fun answerReplayText(answer: String?, busy: Boolean, asr: AsrUiState, tts: AnswerTtsState): String? =
    if (!busy && asr == AsrUiState.Idle && tts == AnswerTtsState.Idle) speakTextForAnswer(answer) else null

/** Respect the system utterance limit without splitting a UTF-16 surrogate pair. */
internal fun answerSpeechChunks(text: String, limit: Int): List<String> {
    require(limit >= 2)
    val chunks = mutableListOf<String>()
    var start = 0
    while (start < text.length) {
        var end = minOf(start + limit, text.length)
        if (end < text.length && text[end - 1].isHighSurrogate() && text[end].isLowSurrogate()) end--
        chunks += text.substring(start, end)
        start = end
    }
    return chunks
}
