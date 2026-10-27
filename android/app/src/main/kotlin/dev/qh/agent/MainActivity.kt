package dev.qh.agent

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
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
import dev.qh.security.SecureTokens
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Manual DI: one repository per retained ViewModel; release cannot select the mock.
        setContent {
            MaterialTheme {
                val model: AgentModel = viewModel(factory = object : ViewModelProvider.Factory {
                    @Suppress("UNCHECKED_CAST")
                    override fun <T : ViewModel> create(modelClass: Class<T>): T = AgentModel(
                        if (BuildConfig.DEBUG && BuildConfig.USE_MOCK_API) MockRepository()
                        else RealRepository(realApi(BuildConfig.API_BASE_URL), SecureTokens(applicationContext))
                    ) as T
                })
                AgentApp(model)
            }
        }
    }
}
class AgentModel(private val repo: Repository) : ViewModel() {
    var signedIn by mutableStateOf(repo.signedIn); private set
    var busy by mutableStateOf(false); private set
    var error by mutableStateOf<String?>(null); private set
    var notes by mutableStateOf(emptyList<Note>()); private set
    var answer by mutableStateOf<Answer?>(null); private set
    var invite by mutableStateOf("")
    var draft by mutableStateOf(""); private set
    var question by mutableStateOf("")
    private var pending: NoteCreate? = null
    private fun run(action: suspend () -> Unit) {
        if (busy) return
        viewModelScope.launch {
            busy = true; error = null
            try { action() }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (!repo.signedIn) { notes = emptyList(); answer = null; draft = ""; question = ""; pending = null }
                error = readableError(e)
            }
            finally { signedIn = repo.signedIn; busy = false }
        }
    }
    fun login() = run { repo.login(invite); invite = "" }
    fun reload() = run { notes = repo.notes() }
    fun updateDraft(value: String) { draft = value; pending = null }
    fun save(done: () -> Unit) = run {
        // Preserve id on uncertain network failure: retry creates at most one server note.
        val body = pending ?: noteDraft(draft).also { pending = it }
        repo.create(body); draft = ""; pending = null; done()
    }
    fun ask() = run { answer = null; answer = repo.ask(question) }
    fun logout() = run {
        try { repo.logout() }
        finally { notes = emptyList(); answer = null; draft = ""; question = ""; pending = null }
    }
}
@Composable
fun AgentApp(model: AgentModel) {
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().imePadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("随记 · S1" + if (BuildConfig.USE_MOCK_API) " / 本地演示" else " / 服务器", style = MaterialTheme.typography.headlineSmall)
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            model.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (!model.signedIn) {
                OutlinedTextField(model.invite, { model.invite = it }, label = { Text("邀请码") },
                    enabled = !model.busy, modifier = Modifier.fillMaxWidth(), singleLine = true)
                Text(if (BuildConfig.USE_MOCK_API) "演示模式：输入任意非空邀请码。记事仅保留在内存中。" else "请输入服务端签发的邀请码。")
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
                                TextButton(model::reload, enabled = !model.busy) { Text("刷新") }
                            }
                            if (model.notes.isEmpty() && !model.busy) Text("还没有记事，试着记录第一条。")
                            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                items(model.notes, key = { it.id }) { note ->
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
