package dev.qh.agent

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.activity.ComponentActivity
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Main-thread only. A fresh recognizer per attempt fences callbacks from older attempts. */
class VoiceAsr(private val activity: ComponentActivity, private val result: (String) -> Unit,
    private val textFallback: String = "文字记事"
) {
    var state by mutableStateOf(AsrUiState.Idle); private set
    var message by mutableStateOf<String?>(null); private set
    private var recognizer: SpeechRecognizer? = null
    private var generation = 0
    private var disposed = false
    private fun event(event: AsrEvent) { state = reduceAsr(state, event) }

    fun start(requestPermission: () -> Unit) {
        if (disposed || state != AsrUiState.Idle) return
        message = null
        if (!SpeechRecognizer.isRecognitionAvailable(activity)) {
            message = "系统语音引擎不可用，请使用${textFallback}。"; return
        }
        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            event(AsrEvent.RequestPermission)
            requestPermission()
        } else {
            event(AsrEvent.Start); listen()
        }
    }
    fun permissionResult(granted: Boolean) {
        if (disposed || state != AsrUiState.Permission) return
        event(if (granted) AsrEvent.Granted else AsrEvent.Denied)
        if (granted) listen() else message = "未授予麦克风权限，请使用${textFallback}。"
    }
    private fun listen() {
        val attempt = ++generation
        try {
            val engine = SpeechRecognizer.createSpeechRecognizer(activity)
            recognizer = engine
            engine.setRecognitionListener(object : RecognitionListener {
                private fun active() = !disposed && generation == attempt && state.acceptsResult()
                override fun onReadyForSpeech(params: Bundle?) {}
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsdB: Float) {}
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() { if (active()) event(AsrEvent.Stop) }
                override fun onError(error: Int) {
                    if (!active()) return
                    cancel()
                    message = when (error) {
                        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "麦克风权限不可用，请使用${textFallback}。"
                        SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "未听清内容，请重试或使用${textFallback}。"
                        SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "语音服务网络异常，请重试或使用${textFallback}。"
                        else -> "系统语音识别失败，请重试或使用${textFallback}。"
                    }
                }
                override fun onResults(results: Bundle?) {
                    if (!active()) return
                    val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        ?.firstOrNull { it.isNotBlank() }?.trim()
                    cancel()
                    if (text == null) message = "未识别到内容，请重试或使用${textFallback}。"
                    else result(text)
                }
                override fun onPartialResults(partialResults: Bundle?) {}
                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
            engine.startListening(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            })
        } catch (_: RuntimeException) {
            cancel(); message = "系统语音引擎不可用，请使用${textFallback}。"
        }
    }
    fun stop() {
        if (state != AsrUiState.Listening) return
        event(AsrEvent.Stop)
        try { recognizer?.stopListening() }
        catch (_: RuntimeException) { cancel(); message = "停止听写失败，请使用${textFallback}。" }
    }
    fun releaseForBackground() {
        // The system permission dialog can pause/stop the activity before delivering its result.
        if (state.shouldCancelOnBackground()) cancel()
    }
    fun cancel() {
        ++generation
        event(AsrEvent.Cancel)
        val old = recognizer
        recognizer = null
        try { old?.cancel() } catch (_: RuntimeException) { }
        try { old?.destroy() } catch (_: RuntimeException) { }
    }
    fun destroy() { disposed = true; cancel() }
}
