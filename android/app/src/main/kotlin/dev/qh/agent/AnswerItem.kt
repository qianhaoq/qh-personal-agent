package dev.qh.agent

import dev.qh.data.Answer
import java.util.UUID

/** Current-process cards only; deliberately excluded from durable composer storage. */
data class AnswerItem(val question: String, val id: String = UUID.randomUUID().toString(),
    val answer: Answer? = null, val failed: Boolean = false)
