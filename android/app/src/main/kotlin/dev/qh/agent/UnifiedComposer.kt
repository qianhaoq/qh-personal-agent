package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.key.*
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.platform.LocalLifecycleOwner

/** One explicit mode and one real recognizer; editable next draft never submits itself while busy. */
@Composable
internal fun UnifiedComposer(model: AgentModel,
    voiceFactory: (String, (String) -> Unit) -> VoiceAsr, requestMicrophone: () -> Unit) {
    var attempt by remember { mutableStateOf<VoiceAttempt?>(null) }
    val onResult by rememberUpdatedState<(String) -> Unit>({ text ->
        attempt?.let { model.acceptVoice(it, text) }
        attempt = null
    })
    val voice = remember { voiceFactory("文字输入") { onResult(it) } }
    LaunchedEffect(voice.state) { model.audioInputState = voice.state }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(voice, lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_PAUSE) voice.releaseForBackground()
        }
        lifecycle.addObserver(observer)
        onDispose { model.audioInputState = AsrUiState.Idle; attempt = null; lifecycle.removeObserver(observer); voice.destroy() }
    }
    Surface(color = MaterialTheme.colorScheme.surfaceVariant, shape = MaterialTheme.shapes.large) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            ComposerModeTabs(model.inputMode) { mode ->
                voice.cancel(); attempt = null; model.setMode(mode)
            }
            OutlinedTextField(model.draft, model::updateDraft,
                label = { Text(if (model.inputMode == "note") "记事内容 · 最多 5000 字" else "问题 · 最多 2000 字") },
                modifier = Modifier.fillMaxWidth().heightIn(max = 180.dp).onPreviewKeyEvent {
                    if (it.type == KeyEventType.KeyUp && it.key == Key.Escape) {
                        voice.cancel(); attempt = null; true
                    } else if (it.type == KeyEventType.KeyUp && it.key == Key.Enter && (it.isCtrlPressed || it.isMetaPressed)) {
                        model.submitComposer(); true
                    } else false
                }, enabled = voice.state == AsrUiState.Idle)
            if (model.busy) Text("上一操作尚未结束；可编辑下一条，不会自动排队。")
            when (voice.state) {
                AsrUiState.Idle -> {
                    OutlinedButton({
                        model.stopAnswerSpeech?.invoke()
                        attempt = runCatching { model.beginVoice() }.getOrNull()
                        if (attempt != null) voice.start(requestMicrophone)
                        model.audioInputState = voice.state
                    }, modifier = Modifier.touchTarget().heightIn(min = 48.dp)) {
                        Text(if (model.inputMode == "note") "开始语音记事" else "开始语音提问")
                    }
                    Button(model::submitComposer,
                        enabled = !model.busy && model.draft.isNotBlank() &&
                            (model.inputMode == "ask" || model.recovery == null),
                        modifier = Modifier.touchTarget().heightIn(min = 48.dp)) {
                        Text(if (model.inputMode == "note") "保存记事" else "提问")
                    }
                }
                else -> ListeningControls(voice.state, voice::stop) { voice.cancel(); attempt = null }

            }
            voice.message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }
}
