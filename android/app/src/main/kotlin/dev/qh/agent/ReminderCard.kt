package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.qh.data.ReminderDto

@Composable
internal fun ReminderCard(reminder: ReminderDto, local: LocalScheduleResult, busy: Boolean,
    retryLocal: () -> Unit, open: (String) -> Unit, confirm: () -> Unit, snooze: () -> Unit,
    terminal: (ReminderAction, ReminderDto) -> Unit) {
    Surface(color = if (reminder.status == "suggested") MaterialTheme.colorScheme.tertiaryContainer
        else MaterialTheme.colorScheme.surfaceVariant, shape = MaterialTheme.shapes.medium) {
        Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(reminder.title, style = MaterialTheme.typography.titleMedium)
            ReminderScheduleStatus(reminder.status, local)
            Text(formatDisplayTimeOrUnset(reminder.fire_at))
            if (reminder.status == "scheduled") {
                Text("系统提交不保证准点送达；通知和频道开关独立。")
                TextButton(retryLocal, enabled = !busy, modifier = Modifier.touchTarget()) { Text("仅重试本机排程") }
            }
            reminderNoteIdForOpen(reminder.note_id)?.let { id ->
                TextButton({ open(id) }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("查看来源记事") }
                if (reminder.status in listOf("suggested", "scheduled")) {
                    TextButton(confirm, enabled = !busy, modifier = Modifier.touchTarget()) { Text("确认提醒 / 修改未来时间") }
                    TextButton(snooze, enabled = !busy, modifier = Modifier.touchTarget()) { Text("10 分钟后提醒") }
                    TextButton({ terminal(ReminderAction.COMPLETE, reminder) }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("完成提醒") }
                    TextButton({ terminal(ReminderAction.CANCEL, reminder) }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("取消提醒") }
                }
            } ?: Text("来源记事不可用；不支持变更")
        }
    }
}

@Composable
internal fun ReminderScheduleStatus(serverStatus: String, local: LocalScheduleResult) {
    Text(reminderStatus(serverStatus))
    if (serverStatus == "scheduled") Text("本机：${local.label}")
}
