package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

@Composable
internal fun ComposerModeTabs(selected: String, change: (String) -> Unit) {
    Row {
        listOf("note" to "记下", "ask" to "提问").forEach { (mode, title) ->
            Tab(modifier = Modifier.touchTarget().weight(1f), selected = selected == mode,
                onClick = { change(mode) }, text = { Text(title) })
        }
    }
}

@Composable
internal fun ListeningControls(state: AsrUiState, stop: () -> Unit, cancel: () -> Unit) {
    Text(when (state) {
        AsrUiState.Permission -> "等待麦克风权限；也可取消后用文字"
        AsrUiState.Listening -> "正在听…"
        else -> "正在识别…"
    })
    if (state == AsrUiState.Listening) Button(stop, modifier = Modifier.touchTarget()) { Text("结束并识别") }
    TextButton(cancel, modifier = Modifier.touchTarget()) { Text("取消本次听写") }
}
