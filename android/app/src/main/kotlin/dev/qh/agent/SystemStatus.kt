package dev.qh.agent

import android.app.AlarmManager
import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver

@Composable
internal fun SystemStatus(model: AgentModel) {
    val context = LocalContext.current
    var revision by remember { mutableIntStateOf(0) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_RESUME) revision++ }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }
    val notifications = context.getSystemService(NotificationManager::class.java)
    val enabled = remember(revision) { notifications.areNotificationsEnabled() }
    val channel = remember(revision) { notifications.getNotificationChannel("reminders") }
    val exact = remember(revision) { Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
        context.getSystemService(AlarmManager::class.java).canScheduleExactAlarms() }
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("同步与权限", style = MaterialTheme.typography.headlineSmall)
        Text("通知总开关：${if (enabled) "开启" else "关闭，可能不显示通知"}")
        Text("提醒频道：${if (channel == null) "尚未创建" else if (channel.importance == NotificationManager.IMPORTANCE_NONE) "关闭" else "开启"}")
        Text("精确闹钟能力：${if (exact) "允许" else "不允许，可尝试非精确排程"}")
        Text("权限、频道、服务端确认与本机提交结果分别表达。系统接受排程不保证准点送达。")
        OutlinedButton({ context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)) }, modifier = Modifier.touchTarget()) { Text("通知设置") }
        Text("麦克风仅在主动听写时请求；拒绝后仍可完整使用文字。系统识别服务可能使用网络。")
        Text("播报只由你主动开启；停止播报不取消服务器请求。")
        Text("首版只恢复本机草稿与创建恢复项。离线冷启动没有服务器内容缓存，未返回的问答无法恢复。")
        TextButton(model::reload, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("立即同步") }
    }
}
