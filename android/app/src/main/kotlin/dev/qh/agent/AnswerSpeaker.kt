package dev.qh.agent

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.util.Locale

/** Main-thread API; engine callbacks are marshalled to main and fenced after stop/destroy. */
class AnswerSpeaker(context: Context) {
    var state by mutableStateOf(AnswerTtsState.Idle); private set
    var message by mutableStateOf<String?>(null); private set
    private val main = Handler(Looper.getMainLooper())
    private var engine: TextToSpeech? = null
    private var ready = false
    private var disposed = false
    private var pending: String? = null
    private var generation = 0L
    private var lastUtterance: String? = null

    init {
        try {
            engine = TextToSpeech(context.applicationContext) { status ->
                main.post {
                    if (!disposed) initialize(status)
                }
            }
        } catch (_: RuntimeException) { unavailable() }
    }

    private fun initialize(status: Int) {
        try {
            val tts = engine
            if (status != TextToSpeech.SUCCESS || tts == null) { unavailable(); return }
            val language = tts.setLanguage(Locale.SIMPLIFIED_CHINESE)
            if (language < TextToSpeech.LANG_AVAILABLE) {
                unavailable("系统 TTS 不支持中文，请阅读文字答案。"); return
            }
            tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) { }
                override fun onDone(utteranceId: String?) = finish(utteranceId, false)
                @Deprecated("Required by Android")
                override fun onError(utteranceId: String?) = finish(utteranceId, true)
            })
            ready = true
            val text = pending
            pending = null
            if (text != null) speak(text)
        } catch (_: RuntimeException) { unavailable() }
    }

    private fun finish(id: String?, failed: Boolean) {
        main.post {
            if (!disposed && id?.startsWith("$generation:") == true) {
                if (failed) {
                    stop()
                    if (state != AnswerTtsState.Unavailable) {
                        state = AnswerTtsState.Idle
                        message = "答案播报失败，请阅读文字答案。"
                    }
                } else if (id == lastUtterance) state = AnswerTtsState.Idle
            }
        }
    }

    private fun unavailable(text: String = "系统 TTS 引擎不可用，请阅读文字答案。") {
        ready = false; pending = null
        state = AnswerTtsState.Unavailable; message = text
    }

    fun speak(text: String) {
        if (disposed) return
        stop()
        val body = speakTextForAnswer(text) ?: return
        if (state == AnswerTtsState.Unavailable) return
        message = null
        if (!ready) { pending = body; state = AnswerTtsState.Speaking; return }
        try {
            val chunks = answerSpeechChunks(body, TextToSpeech.getMaxSpeechInputLength())
            lastUtterance = "$generation:${chunks.lastIndex}"
            state = AnswerTtsState.Speaking
            for ((index, chunk) in chunks.withIndex()) {
                val mode = if (index == 0) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
                if (engine?.speak(chunk, mode, null, "$generation:$index") != TextToSpeech.SUCCESS) {
                    stop()
                    message = "答案播报失败，请阅读文字答案。"
                    return
                }
            }
        } catch (_: RuntimeException) { stop(); unavailable() }
    }

    fun stop() {
        ++generation; pending = null; lastUtterance = null
        try { engine?.stop() } catch (_: RuntimeException) { }
        if (state != AnswerTtsState.Unavailable) state = AnswerTtsState.Idle
    }

    fun releaseForBackground() = stop()
    fun destroy() {
        if (disposed) return
        disposed = true
        stop()
        try { engine?.shutdown() } catch (_: RuntimeException) { }
        engine = null; ready = false
    }
}
