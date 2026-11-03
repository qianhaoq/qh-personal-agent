package dev.qh.agent

fun speakTextForAnswer(answer: String?): String? = answer?.trim()?.takeIf { it.isNotEmpty() }

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
