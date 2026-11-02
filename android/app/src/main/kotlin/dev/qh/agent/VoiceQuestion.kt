package dev.qh.agent

data class VoiceQuestion(val question: String, val shouldAutoAsk: Boolean)

// Keep the transcript available for manual retry even while another request is running.
fun voiceQuestion(text: String, busy: Boolean) = VoiceQuestion(text, shouldAutoAsk = !busy)
