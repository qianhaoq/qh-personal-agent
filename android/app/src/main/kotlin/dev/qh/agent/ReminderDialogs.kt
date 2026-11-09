package dev.qh.agent

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.qh.data.ReminderDto
import dev.qh.data.readableError

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ReminderConfirmationDialog(
    reminder: ReminderDto, busy: Boolean, title: String, fireAt: String,
    submitError: String?,
    onTitleChange: (String) -> Unit, onFireAtChange: (String) -> Unit,
    onDismiss: () -> Unit, onConfirm: (ConfirmOverrides) -> Unit
) {
    var error by remember(reminder) { mutableStateOf<String?>(null) }
    var choosing by remember(reminder) { mutableStateOf(false) }
    val zone = java.time.ZoneId.systemDefault()
    val initial = remember(reminder) {
        runCatching { java.time.Instant.parse(fireAt).atZone(zone) }.getOrElse { java.time.ZonedDateTime.now(zone) }
    }
    val date = rememberDatePickerState(initialSelectedDateMillis = initial.toLocalDate()
        .atStartOfDay(java.time.ZoneOffset.UTC).toInstant().toEpochMilli())
    val time = rememberTimePickerState(initial.hour, initial.minute, is24Hour = true)
    if (choosing) Dialog(onDismissRequest = { choosing = false }) {
        Surface(shape = MaterialTheme.shapes.extraLarge, color = MaterialTheme.colorScheme.surface) {
            // The window bounds the whole column. Reserve the footer before measuring
            // the scroll viewport, so its bottom is also the body's visible bottom.
            Column(Modifier.heightIn(max = 600.dp)) {
                Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState())) {
                    DatePicker(date)
                    TimeInput(time)
                    Text("时区：${zone.id}", modifier = Modifier.padding(16.dp))
                }
                Column(Modifier.fillMaxWidth().padding(8.dp), horizontalAlignment = Alignment.End) {
                    TextButton({
                        date.selectedDateMillis?.let { millis ->
                            val day = java.time.Instant.ofEpochMilli(millis).atZone(java.time.ZoneOffset.UTC).toLocalDate()
                            onFireAtChange(day.atTime(time.hour, time.minute).atZone(zone).toInstant().toString())
                        }
                        choosing = false
                    }, modifier = Modifier.touchTarget()) { Text("使用所选本地时间") }
                    TextButton({ choosing = false }, modifier = Modifier.touchTarget()) { Text("取消") }
                }
            }
        }
    }
    // Keep a single modal window: the confirmation dialog otherwise covers the picker
    // body and intercepts scrolling even though its semantics remain discoverable.
    if (!choosing) AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("确认提醒 / 修改未来时间") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(title, { onTitleChange(it); error = null }, label = { Text("标题") },
                    enabled = !busy, modifier = Modifier.fillMaxWidth())
                Text("目标时间：${formatDisplayTimeOrUnset(fireAt)}")
                OutlinedButton({ choosing = true }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("修改日期与时间") }
                Text("时间须在未来。服务端确认与本机系统排程分别报告；不保证准点送达。")
                (error ?: submitError)?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = {
            TextButton({
                try {
                    val overrides = confirmOverrides(reminder.title, reminder.fire_at, title, fireAt)
                    require(java.time.Instant.parse(overrides.fireAt ?: reminder.fire_at).isAfter(java.time.Instant.now())) { "时间已过，请修改为未来时间" }
                    onConfirm(overrides)
                } catch (e: IllegalArgumentException) { error = readableError(e) }
            }, enabled = !busy, modifier = Modifier.touchTarget()) { Text("确认") }
        },
        dismissButton = { TextButton(onDismiss, enabled = !busy, modifier = Modifier.touchTarget()) { Text("取消") } }
    )
}

@Composable
internal fun ReminderActionConfirmDialog(action: ReminderAction, reminderTitle: String, busy: Boolean, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    val enabled = shouldAcceptReminderActionConfirm(busy)
    AlertDialog(onDismissRequest = { if (enabled) onDismiss() },
        title = { Text(reminderActionConfirmTitle(action)) },
        text = { Text(reminderActionConfirmMessage(action, reminderTitle)) },
        confirmButton = {
            TextButton({ if (enabled) onConfirm() }, enabled = enabled, modifier = Modifier.touchTarget()) { Text("确认") }
        }, dismissButton = {
            TextButton({ if (enabled) onDismiss() }, enabled = enabled, modifier = Modifier.touchTarget()) { Text("取消") }
        })
}
