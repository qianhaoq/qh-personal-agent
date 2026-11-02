package dev.qh.agent

import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewModelScope
import androidx.navigation.compose.*
import dev.qh.data.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notificationPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        // Share the real session across foreground and boot work; release cannot select the mock.
        setContent {
            MaterialTheme {
                val model: AgentModel = viewModel(factory = object : ViewModelProvider.Factory {
                    @Suppress("UNCHECKED_CAST")
                    override fun <T : ViewModel> create(modelClass: Class<T>): T = AgentModel(
                        if (BuildConfig.DEBUG && BuildConfig.USE_MOCK_API) MockRepository()
                        else AppRepositories.real(applicationContext),
                        ReminderAlarmScheduler(applicationContext)
                    ) as T
                })
                AgentApp(model)
            }
        }
    }
}
class AgentModel(private val repo: Repository, private val alarms: ReminderAlarmScheduler) : ViewModel() {
    var signedIn by mutableStateOf(repo.signedIn); private set
    var busy by mutableStateOf(false); private set
    var error by mutableStateOf<String?>(null); private set
    var notes by mutableStateOf(emptyList<Note>()); private set
    var reminders by mutableStateOf(emptyList<ReminderDto>()); private set
    var answer by mutableStateOf<Answer?>(null); private set
    var invite by mutableStateOf("")
    var draft by mutableStateOf(""); private set
    var question by mutableStateOf("")
    private var pending: NoteCreate? = null
    private fun clearSession() {
        notes = emptyList(); reminders = emptyList(); answer = null; draft = ""; question = ""; pending = null
        AppRepositories.bumpSession()
        alarms.clear()
    }
    private fun run(action: suspend () -> Unit) {
        if (busy) return
        viewModelScope.launch {
            busy = true; error = null
            try { action() }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (!repo.signedIn) clearSession()
                error = readableError(e)
            }
            finally { signedIn = repo.signedIn; busy = false }
        }
    }
    private suspend fun refresh() {
        val generation = AppRepositories.sessionGeneration()
        val sync = repo.sync()
        if (!repo.signedIn || AppRepositories.sessionGeneration() != generation) {
            if (!repo.signedIn) clearSession()
            return
        }
        alarms.consume(sync)
        reminders = sync.reminders.sortedWith(compareBy(nullsLast()) { it.fire_at })
        notes = repo.notes()
    }
    fun login() = run { repo.login(invite); invite = "" }
    fun reload() = run { refresh() }
    fun confirmReminder(reminder: ReminderDto) = run {
        val confirmed = repo.confirmReminder(reminder.id, reminder.rev)
        alarms.confirmed(confirmed)
        refresh()
    }
    fun updateDraft(value: String) { draft = value; pending = null }
    fun save(done: () -> Unit) = run {
        // Preserve id on uncertain network failure: retry creates at most one server note.
        val body = pending ?: noteDraft(draft).also { pending = it }
        repo.create(body); draft = ""; pending = null; done()
    }
    fun ask() = run { answer = null; answer = repo.ask(question) }
    fun logout() = run {
        // Bump generation and clear alarms before network logout so in-flight boot cannot re-arm.
        AppRepositories.bumpSession()
        alarms.clear()
        try { repo.logout() }
        finally { clearSession() }
    }
}
@Composable
fun AgentApp(model: AgentModel) {
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().imePadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("随记 · S2" + if (BuildConfig.USE_MOCK_API) " / 本地演示" else " / 服务器", style = MaterialTheme.typography.headlineSmall)
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            model.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (!model.signedIn) {
                OutlinedTextField(model.invite, { model.invite = it }, label = { Text("邀请码") },
                    enabled = !model.busy, modifier = Modifier.fillMaxWidth(), singleLine = true)
                Text(if (BuildConfig.USE_MOCK_API) "演示模式：输入任意非空邀请码。记事与提醒仅保留在内存中。" else "请输入服务端签发的邀请码。")
                Button(model::login, enabled = !model.busy && model.invite.isNotBlank()) { Text("登录") }
            } else {
                val nav = rememberNavController()
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton({ nav.navigate("timeline") { popUpTo("timeline") { inclusive = true } } }, enabled = !model.busy) { Text("时间线") }
                    TextButton(model::logout, enabled = !model.busy) { Text("退出") }
                }
                NavHost(nav, startDestination = "timeline", modifier = Modifier.weight(1f)) {
                    composable("timeline") {
                        LaunchedEffect(Unit) { model.reload() }
                        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button({ nav.navigate("capture") }, enabled = !model.busy) { Text("文字记事") }
                                Button({ nav.navigate("ask") }, enabled = !model.busy) { Text("问一问") }
                                TextButton(model::reload, enabled = !model.busy) { Text("同步") }
                            }
                            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                item { Text("提醒", style = MaterialTheme.typography.titleMedium) }
                                if (model.reminders.isEmpty() && !model.busy) item { Text("暂无提醒。") }
                                items(model.reminders, key = { "reminder:${it.id}" }) { reminder ->
                                    Card(Modifier.fillMaxWidth()) {
                                        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                            Text(reminder.title, style = MaterialTheme.typography.titleMedium)
                                            Text("时间：${reminder.fire_at ?: "未设置"}")
                                            Text("状态：${reminderStatus(reminder.status)} · rev ${reminder.rev}",
                                                style = MaterialTheme.typography.bodySmall)
                                            if (reminder.status == "suggested") {
                                                Button({ model.confirmReminder(reminder) },
                                                    enabled = !model.busy && reminder.fire_at != null) { Text("确认并设闹钟") }
                                            }
                                        }
                                    }
                                }
                                item { Text("记事", style = MaterialTheme.typography.titleMedium) }
                                if (model.notes.isEmpty() && !model.busy) item { Text("还没有记事，试着记录第一条。") }
                                items(model.notes, key = { "note:${it.id}" }) { note ->
                                    Card(Modifier.fillMaxWidth()) {
                                        Column(Modifier.padding(12.dp)) {
                                            note.title?.let { Text(it, style = MaterialTheme.typography.titleMedium) }
                                            Text(note.text)
                                            Text("${note.recorded_at.orEmpty()} · ${note.status}", style = MaterialTheme.typography.bodySmall)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    composable("capture") {
                        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            OutlinedTextField(model.draft, model::updateDraft, label = { Text("记事内容（最多 5000 字）") },
                                modifier = Modifier.fillMaxWidth().weight(1f), enabled = !model.busy)
                            OutlinedButton({}, enabled = false) { Text("录音 · 后续阶段提供") }
                            Button({ model.save { nav.popBackStack() } }, enabled = !model.busy && model.draft.isNotBlank()) { Text("保存") }
                        }
                    }
                    composable("ask") {
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            item {
                                OutlinedTextField(model.question, { model.question = it }, label = { Text("问题（最多 2000 字）") },
                                    modifier = Modifier.fillMaxWidth(), enabled = !model.busy)
                                Button(model::ask, enabled = !model.busy && model.question.isNotBlank()) { Text("提问") }
                            }
                            model.answer?.let { answer ->
                                item { Text(answer.answer) }
                                items(answer.citations) { Text("来源 ${it.note_id}：${it.text}") }
                            }
                        }
                    }
                }
            }
        }
    }
}
private fun reminderStatus(status: String) = when (status) {
    "suggested" -> "待确认"
    "scheduled" -> "已确认"
    "done" -> "已完成"
    "cancelled" -> "已取消"
    else -> status
}
