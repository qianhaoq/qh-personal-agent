package dev.qh.agent

import android.content.Context
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/** Permission is device-wide. Denial/dismissal does not trigger another automatic request. */
internal class NotificationPrompt(context: Context) {
    private val prefs = context.getSharedPreferences("notification_explanation", Context.MODE_PRIVATE)
    fun claim(): Boolean = synchronized(lock) {
        if (prefs.getBoolean("explained", false)) false
        else prefs.edit().putBoolean("explained", true).commit()
    }
    private companion object { val lock = Any() }
}

@Composable
internal fun NotificationPermissionExplanation(dismiss: () -> Unit, request: () -> Unit) {
    AlertDialog(onDismissRequest = dismiss,
        title = { Text("允许显示提醒通知？") },
        text = { Text("本机已提交系统排程。通知权限和频道开关决定通知是否可见；拒绝后可在同步与权限页手动开启。") },
        confirmButton = { TextButton(request, modifier = Modifier.touchTarget()) { Text("继续授权") } },
        dismissButton = { TextButton(dismiss, modifier = Modifier.touchTarget()) { Text("暂不") } })
}
