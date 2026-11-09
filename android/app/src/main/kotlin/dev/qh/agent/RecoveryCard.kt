package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.qh.data.CreateRecovery
import dev.qh.data.Note

internal fun recoveryMessage(state: String) = when (state) {
    "live" -> "原记事已核对；下一草稿独立保留"
    "rejected" -> "本次创建未被接受；原请求仍在本机"
    "deleted" -> "原记事不存在或已删除；下一草稿仍保留"
    "sending" -> "正在保存原文；下一草稿独立保留"
    else -> "保存结果未知；服务器可能已保存原请求"
}

@Composable
internal fun RecoveryCard(recovery: CreateRecovery, authoritative: Note?, draft: String, busy: Boolean,
    reconcile: () -> Unit, retry: () -> Unit, update: () -> Unit, saveCopy: () -> Unit, remove: () -> Unit) {
    var removing by remember(recovery.operationId) { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(recoveryMessage(recovery.state))
            Text(authoritative?.text ?: recovery.request.text)
            if (recovery.state !in listOf("deleted", "rejected"))
                TextButton(reconcile, enabled = !busy, modifier = Modifier.touchTarget()) { Text("核对原记事") }
            if (recovery.state == "unknown")
                TextButton(retry, enabled = !busy, modifier = Modifier.touchTarget()) { Text("原样重试原请求") }
            if (recovery.state == "live" && authoritative?.id == recovery.discoveredNoteId && draft.isNotBlank()) {
                TextButton(update, enabled = !busy, modifier = Modifier.touchTarget()) { Text("用当前草稿更新此记事") }
                TextButton(saveCopy, enabled = !busy, modifier = Modifier.touchTarget()) { Text("将草稿另存为新记事，保留原记事") }
            }
            TextButton({ removing = true }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("移除本机恢复项，保留下一草稿") }
        }
    }
    if (removing) AlertDialog(onDismissRequest = { removing = false },
        title = { Text("只移除本机恢复项？") },
        text = { Text("不会删除或撤销服务器可能已保存的原记事。当前草稿保留，之后保存会使用新的创建身份。") },
        confirmButton = { TextButton({ removing = false; remove() }, modifier = Modifier.touchTarget()) { Text("只移除本机恢复项") } },
        dismissButton = { TextButton({ removing = false }, modifier = Modifier.touchTarget()) { Text("取消") } })
}
