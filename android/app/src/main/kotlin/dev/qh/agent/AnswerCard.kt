package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
internal fun AnswerCard(item: AnswerItem, busy: Boolean, asr: AsrUiState, tts: AnswerTtsState,
    speak: (String) -> Unit, stop: () -> Unit, retry: () -> Unit, open: (String) -> Unit,
    search: () -> Unit, message: String?) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text("你问：${item.question}", style = MaterialTheme.typography.labelLarge)
            val result = item.answer
            if (result != null) {
                var expanded by remember(item.id) { mutableStateOf(false) }
                var overflows by remember(item.id) { mutableStateOf(false) }
                Text(result.answer, maxLines = if (expanded) Int.MAX_VALUE else 8,
                    onTextLayout = { if (!expanded) overflows = it.hasVisualOverflow })
                if (expanded || overflows) TextButton({ expanded = !expanded }, modifier = Modifier.touchTarget()) {
                    Text(if (expanded) "收起答案" else "展开完整答案")
                }
                val replay = answerReplayText(result.answer, busy, asr, tts)
                TextButton({ replay?.let(speak) }, enabled = replay != null, modifier = Modifier.touchTarget()) {
                    Text(if (tts == AnswerTtsState.Speaking) "正在播报" else "播报答案")
                }
                TextButton(stop, modifier = Modifier.touchTarget()) { Text("停止本机播报") }
                message?.let { Text(it) }
                if (result.citations.isEmpty()) TextButton(search, modifier = Modifier.touchTarget()) { Text("去搜记忆") }
                result.citations.forEachIndexed { index, citation ->
                    Text("来源 ${index + 1}：${citation.text}")
                    citation.note_id?.takeIf { it.isNotBlank() }?.let { id ->
                        TextButton({ open(id) }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("查看原记事") }
                    } ?: Text("来源记事不可用")
                }
            } else if (item.failed) {
                Text("这次未拿到答案；不会自动重试。")
                TextButton(retry, enabled = !busy, modifier = Modifier.touchTarget()) { Text("重新提问") }
            } else Text("正在查找记忆并回答…")
        }
    }
}
