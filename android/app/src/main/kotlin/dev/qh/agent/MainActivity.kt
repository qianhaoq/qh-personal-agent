package dev.qh.agent

import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewModelScope
import androidx.navigation.compose.*
import dev.qh.data.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class MainActivity : ComponentActivity() {
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private var voice: VoiceAsr? = null
    private val microphonePermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        voice?.permissionResult(it)
    }
    override fun onStop() { voice?.releaseForBackground(); super.onStop() }
    override fun onDestroy() { voice?.destroy(); voice = null; super.onDestroy() }
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
                AgentApp(model, { fallback, callback -> VoiceAsr(this, callback, fallback).also { voice = it } }, {
                    microphonePermission.launch(android.Manifest.permission.RECORD_AUDIO)
                }, { voice?.cancel() })
            }
        }
    }
}
class AgentModel(private val repo: Repository, private val alarms: ReminderAlarmScheduler) : ViewModel() {
    var signedIn by mutableStateOf(repo.signedIn); private set
    var busy by mutableStateOf(false); private set
    var error by mutableStateOf<String?>(null); private set
    var notes by mutableStateOf(emptyList<Note>()); private set
    var detailNote by mutableStateOf<Note?>(null); private set
    fun clearDetail() { detailNote = null }
    fun openNote(noteId: String) = run { clearDetail(); detailNote = repo.note(noteId) }
    var reminders by mutableStateOf(emptyList<ReminderDto>()); private set
    private var showEndedState by mutableStateOf(false)
    val showEnded: Boolean get() = showEndedState
    fun setShowEnded(value: Boolean) { showEndedState = value }
    var answer by mutableStateOf<Answer?>(null); private set
    var invite by mutableStateOf("")
    var draft by mutableStateOf(""); private set
    var searchQuery by mutableStateOf("")
    var searchHits by mutableStateOf(emptyList<SearchHit>()); private set
    var searchCompleted by mutableStateOf(false); private set
    var question by mutableStateOf("")
    var organizingNoteIds by mutableStateOf(emptySet<String>()); private set
    private val organizeJobs = mutableMapOf<String, Job>()
    private val refreshMutex = Mutex()
    private val unsubscribeRemindersMutated = AppRepositories.subscribeRemindersMutated { generation ->
        viewModelScope.launch {
            try {
                refresh(expectedGeneration = generation)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                AppRepositories.ifCurrent(generation) {
                    if (!repo.signedIn) { clearSession(); signedIn = false }
                    error = readableError(e)
                }
            }
        }
    }

    override fun onCleared() {
        unsubscribeRemindersMutated()
        super.onCleared()
    }

    private var pending: NoteCreate? = null
    private var draftSource = "app"
    var onAnswer: ((String) -> Unit)? = null
    var stopAnswerSpeech: (() -> Unit)? = null
    private fun clearUi() {
        stopAnswerSpeech?.invoke()
        organizeJobs.values.toList().forEach { it.cancel() }; organizeJobs.clear()
        organizingNoteIds = emptySet()
        clearDetail()
        editing = null; editText = ""
        cancelConfirm()
        searchQuery = ""; searchHits = emptyList(); searchCompleted = false
        notes = emptyList(); reminders = emptyList(); answer = null; draft = ""; question = ""; pending = null; draftSource = "app"
    }
    private fun clearSession() {
        // Fence first so in-flight boot/foreground sync cannot re-apply alarms.
        AppRepositories.bumpSession()
        alarms.clear()
        clearUi()
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
    private suspend fun refresh(expectedGeneration: Long? = null) = refreshMutex.withLock {
        // Recheck after waiting for another refresh, before any repository/network work.
        val generation = expectedGeneration ?: AppRepositories.sessionGeneration()
        if (expectedGeneration != null &&
            (!repo.signedIn || AppRepositories.sessionGeneration() != generation)) return@withLock
        val alarmGeneration = AppRepositories.alarmGeneration()
        val sync = repo.sync()
        if (!repo.signedIn || AppRepositories.sessionGeneration() != generation) {
            if (!repo.signedIn) clearSession()
            return@withLock
        }
        AppRepositories.consumeIfCurrent(generation, alarmGeneration) {
            if (repo.signedIn) {
                alarms.consume(sync)
                reminders = sync.reminders.sortedWith(compareBy(nullsLast()) { it.fire_at })
            }
        }
        val fresh = repo.notes()
        if (repo.signedIn && AppRepositories.sessionGeneration() == generation) {
            notes = fresh
            detailNote = syncedDetailNote(detailNote, fresh)
        }
    }
    fun login() = run { repo.login(invite); AppRepositories.bumpSession(); invite = "" }
    fun reload() = run { refresh() }
    fun confirmReminder(reminder: ReminderDto, overrides: ConfirmOverrides) = run {
        val confirmed = repo.confirmReminder(reminder.id, reminder.rev, overrides.fireAt, overrides.title)
        cancelConfirm()
        alarms.confirmed(confirmed)
        refresh()
    }
    fun cancelReminder(reminder: ReminderDto) = run {
        repo.cancelReminder(reminder.id, reminder.rev)
        AppRepositories.invalidateAlarms {
            alarms.cancelReminder(reminder.id)
        }
        refresh()
    }
    fun completeReminder(reminder: ReminderDto) = run {
        repo.completeReminder(reminder.id, reminder.rev)
        AppRepositories.invalidateAlarms {
            alarms.cancelReminder(reminder.id)
        }
        refresh()
    }
    fun snoozeReminder(reminder: ReminderDto) = run {
        val response = repo.snoozeReminder(reminder.id, reminder.rev, minutes = 10)
        AppRepositories.invalidateAlarms {
            alarms.cancelReminder(reminder.id)
            alarms.snoozed(response)
        }
        refresh()
    }
    fun updateDraft(value: String) { draft = value; draftSource = "app"; pending = null }
    fun createFromVoice(text: String, done: () -> Unit) {
        if (busy) return
        draft = text; draftSource = "voice_asr"; pending = null
        save(done)
    }
    fun save(done: () -> Unit) = run {
        // Preserve id on uncertain network failure: retry creates at most one server note.
        val body = pending ?: noteDraft(draft, source = draftSource).also { pending = it }
        val created = repo.create(body)
        draft = ""; pending = null; draftSource = "app"
        notes = listOf(Note(created.note_id, body.text, created.status, created.rev,
            recorded_at = body.recorded_at)) + notes.filterNot { it.id == created.note_id }
        done()
        waitForOrganize(created)
    }
    var confirming by mutableStateOf<ReminderDto?>(null); private set
    var confirmTitle by mutableStateOf(""); private set
    var confirmFireAt by mutableStateOf(""); private set
    fun beginConfirm(reminder: ReminderDto) {
        error = null
        confirming = reminder; confirmTitle = reminder.title; confirmFireAt = reminder.fire_at.orEmpty()
    }
    fun updateConfirmTitle(value: String) { confirmTitle = value; error = null }
    fun updateConfirmFireAt(value: String) { confirmFireAt = value; error = null }
    fun cancelConfirm() { confirming = null; confirmTitle = ""; confirmFireAt = ""; error = null }
    var editing by mutableStateOf<Note?>(null); private set
    var editText by mutableStateOf(""); private set
    fun beginEdit(note: Note) { editing = note; editText = note.text }
    fun updateEdit(value: String) { editText = value }
    fun saveEdit(done: () -> Unit) = run {
        val original = editing ?: return@run
        try {
            val updated = refreshMutex.withLock {
                repo.patch(original.id, original.rev, editText).also { note ->
                    notes = notes.map { if (it.id == note.id) note else it }
                    if (detailNote?.id == note.id) detailNote = note
                }
            }
            editing = null; editText = ""
            done()
            if (shouldPollOrganizeAfterEdit(updated.status)) {
                organizeJobs[updated.id]?.cancelAndJoin()
                waitForOrganize(Created(updated.id, updated.status, updated.rev))
            }
            refresh()
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if ((e is retrofit2.HttpException && e.code() == 409) ||
                (e is IllegalArgumentException && e.message?.contains("版本冲突") == true)) {
                error = readableError(e)
                refresh()
                editing = notes.find { it.id == original.id }
            } else throw e
        }
    }
    fun deleteNote(note: Note, done: () -> Unit = {}) = run {
        refreshMutex.withLock {
            repo.delete(note.id)
            AppRepositories.invalidateAlarms {
                alarms.consume(SyncResponse(0, deleted = SyncDeleted(
                    notes = listOf(note.id),
                    reminders = reminders.filter { it.note_id == note.id }.map { it.id })))
            }
            reminders = reminders.filterNot { it.note_id == note.id }
            organizeJobs.remove(note.id)?.cancel()
            organizingNoteIds = organizingNoteIds - note.id
            notes = notes.filterNot { it.id == note.id }
            if (detailNote?.id == note.id) clearDetail()
        }
        done()
        refresh()
    }
    private fun waitForOrganize(created: Created) {
        val id = created.note_id
        val generation = AppRepositories.sessionGeneration()
        organizingNoteIds = organizingNoteIds + id
        organizeJobs[id] = viewModelScope.launch {
            try {
                withTimeoutOrNull(ORGANIZE_POLL_TIMEOUT_MS) {
                    val start = SystemClock.elapsedRealtime()
                    var attempt = 0
                    var status: String? = created.status
                    while (shouldContinueOrganizePoll(status, attempt, SystemClock.elapsedRealtime() - start)) {
                        delay(nextOrganizePollDelayMs(attempt))
                        refreshMutex.withLock {
                            val fresh = repo.notes()
                            if (!repo.signedIn || AppRepositories.sessionGeneration() != generation) return@withTimeoutOrNull
                            notes = fresh
                            detailNote = syncedDetailNote(detailNote, fresh)
                            status = fresh.find { it.id == id }?.status
                        }
                        attempt++
                    }
                }
                requireSessionAfterOrganizePoll(AppRepositories.sessionGeneration() == generation, repo.signedIn)
                if (repo.signedIn && AppRepositories.sessionGeneration() == generation) refresh()
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (AppRepositories.sessionGeneration() == generation) {
                    if (!repo.signedIn) { clearSession(); signedIn = false }
                    error = readableError(e)
                }
            } finally {
                organizingNoteIds = organizingNoteIds - id
                organizeJobs.remove(id)
            }
        }
    }
    fun askFromVoice(text: String) {
        val result = voiceQuestion(text, busy)
        question = result.question
        if (result.shouldAutoAsk) ask()
    }
    fun search() = run {
        searchCompleted = false; searchHits = emptyList()
        searchHits = repo.search(searchQuery); searchCompleted = true
    }
    fun ask() = run {
        stopAnswerSpeech?.invoke()
        answer = null
        answer = repo.ask(question).also { result ->
            speakTextForAnswer(result.answer)?.let { onAnswer?.invoke(it) }
        }
    }
    fun logout() = run {
        stopAnswerSpeech?.invoke()
        // Bump generation + clear alarms before network logout so in-flight boot cannot re-arm.
        AppRepositories.bumpSession()
        organizeJobs.values.toList().forEach { it.cancel() }; organizeJobs.clear()
        organizingNoteIds = emptySet()
        alarms.clear()
        try { repo.logout() }
        finally { clearUi() }
    }
}
@Composable
fun AgentApp(model: AgentModel, voiceFactory: (String, (String) -> Unit) -> VoiceAsr, requestMicrophone: () -> Unit, cancelVoice: () -> Unit) {
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().imePadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("随记 · S26" + if (BuildConfig.USE_MOCK_API) " / 本地演示" else " / 服务器", style = MaterialTheme.typography.headlineSmall)
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            model.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (!model.signedIn) {
                OutlinedTextField(model.invite, { model.invite = it }, label = { Text("邀请码") },
                    enabled = !model.busy, modifier = Modifier.fillMaxWidth(), singleLine = true)
                Text(if (BuildConfig.USE_MOCK_API) "演示模式：输入任意非空邀请码。记事与提醒仅保留在内存中。" else "请输入服务端签发的邀请码。")
                Button(model::login, enabled = !model.busy && model.invite.isNotBlank()) { Text("登录") }
            } else {
                val nav = rememberNavController()
                val openDetail: (String) -> Unit = { id -> model.openNote(id); nav.navigate("detail") }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton({ cancelVoice(); nav.navigate("timeline") { popUpTo("timeline") { inclusive = true } } }, enabled = !model.busy) { Text("时间线") }
                    TextButton({ cancelVoice(); model.logout() }, enabled = !model.busy) { Text("退出") }
                }
                NavHost(nav, startDestination = "timeline", modifier = Modifier.weight(1f)) {
                    composable("timeline") {
                        var pendingReminderAction by remember { mutableStateOf<Pair<ReminderAction, ReminderDto>?>(null) }
                        pendingReminderAction?.let { (action, reminder) ->
                            ReminderActionConfirmDialog(action, model.busy,
                                onDismiss = { pendingReminderAction = null },
                                onConfirm = {
                                    pendingReminderAction = null
                                    when (action) {
                                        ReminderAction.COMPLETE -> model.completeReminder(reminder)
                                        ReminderAction.CANCEL -> model.cancelReminder(reminder)
                                    }
                                })
                        }
                        val visibleReminders = filterRemindersForList(model.reminders, model.showEnded)
                        model.confirming?.let { reminder ->
                            ConfirmReminderDialog(reminder, model.busy,
                                title = model.confirmTitle, fireAt = model.confirmFireAt,
                                submitError = model.error,
                                onTitleChange = model::updateConfirmTitle,
                                onFireAtChange = model::updateConfirmFireAt,
                                onDismiss = model::cancelConfirm,
                                onConfirm = { overrides -> model.confirmReminder(reminder, overrides) })
                        }
                        var deleting by remember { mutableStateOf<Note?>(null) }
                        deleting?.let { note ->
                            AlertDialog(onDismissRequest = { if (!model.busy) deleting = null },
                                title = { Text("删除记事？") }, text = { Text("相关提醒也会删除，此操作不可撤销。") },
                                confirmButton = {
                                    TextButton({ deleting = null; model.deleteNote(note) }, enabled = !model.busy) { Text("删除") }
                                }, dismissButton = {
                                    TextButton({ deleting = null }, enabled = !model.busy) { Text("取消") }
                                })
                        }
                        LaunchedEffect(Unit) { model.reload() }
                        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button({ nav.navigate("capture") }, enabled = !model.busy) { Text("记一条") }
                                Button({ nav.navigate("ask") }, enabled = !model.busy) { Text("问一问") }
                                Button({ nav.navigate("search") }, enabled = !model.busy) { Text("搜记忆") }
                            }
                            TextButton(model::reload, enabled = !model.busy) { Text("同步") }
                            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                item {
                                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp),
                                        verticalAlignment = Alignment.CenterVertically) {
                                        Text("提醒", style = MaterialTheme.typography.titleMedium)
                                        FilterChip(selected = model.showEnded,
                                            onClick = { model.setShowEnded(!model.showEnded) },
                                            label = { Text("显示已结束") })
                                    }
                                }
                                if (visibleReminders.isEmpty() && !model.busy) item { Text(reminderListEmptyMessage(model.reminders, model.showEnded)) }
                                items(visibleReminders, key = { "reminder:${it.id}" }) { reminder ->
                                    Card(Modifier.fillMaxWidth()) {
                                        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                            Text(reminder.title, style = MaterialTheme.typography.titleMedium)
                                            ReminderStatusBadge(reminder.status)
                                            Text("时间：${formatDisplayTimeOrUnset(reminder.fire_at)}")
                                            Text("rev ${reminder.rev}",
                                                style = MaterialTheme.typography.bodySmall)
                                            reminderNoteIdForOpen(reminder.note_id)?.let { id ->
                                                TextButton({ openDetail(id) }, enabled = !model.busy) { Text("打开记事") }
                                            }
                                            if (reminder.status in listOf("suggested", "scheduled")) {
                                                TextButton({ pendingReminderAction = ReminderAction.CANCEL to reminder },
                                                    enabled = !model.busy) { Text("取消提醒") }
                                                TextButton({ pendingReminderAction = ReminderAction.COMPLETE to reminder },
                                                    enabled = !model.busy) { Text("完成提醒") }
                                            }
                                            if (reminderCanSnooze(reminder.status)) {
                                                TextButton({ model.snoozeReminder(reminder) },
                                                    enabled = !model.busy) { Text("10分钟后") }
                                            }
                                            if (reminder.status == "suggested") {
                                                Button({ model.beginConfirm(reminder) },
                                                    enabled = !model.busy) { Text("确认并设闹钟") }
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
                                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                                TextButton({ openDetail(note.id) }, enabled = !model.busy) { Text("详情") }
                                                TextButton({ model.beginEdit(note); nav.navigate("edit") }, enabled = !model.busy) { Text("编辑") }
                                                TextButton({ deleting = note }, enabled = !model.busy) { Text("删除") }
                                            }
                                            Text(note.text)
                                            if (note.status == "uploaded") {
                                                Text("整理中…", style = MaterialTheme.typography.bodySmall,
                                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                            }
                                            note.tags?.takeIf { it.isNotEmpty() }?.let { Text("标签：${it.joinToString(", ")}") }
                                            note.todos.orEmpty().forEach { todo ->
                                                Text("• ${todo.text}" + (formatDisplayTime(todo.due).takeIf { it.isNotEmpty() }?.let { "（$it）" } ?: ""))
                                            }
                                            note.category?.let { Text("分类：$it") }
                                            Text("${formatDisplayTime(note.recorded_at)} · ${noteStatus(note.status)}", style = MaterialTheme.typography.bodySmall)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    composable("search") {
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            item {
                                OutlinedTextField(model.searchQuery, { model.searchQuery = it },
                                    label = { Text("搜索记忆（最多 500 字）") },
                                    modifier = Modifier.fillMaxWidth(), enabled = !model.busy)
                                Button(model::search, enabled = !model.busy && model.searchQuery.isNotBlank()) { Text("搜索") }
                            }
                            if (model.searchCompleted && model.searchHits.isEmpty() && !model.busy) item { Text("没有找到相关记忆。") }
                            items(model.searchHits) { hit ->
                                Card(Modifier.fillMaxWidth()) {
                                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                        Text(hit.text)
                                        hit.note_id?.takeIf { it.isNotBlank() }?.let { id ->
                                            Text("记事：$id", style = MaterialTheme.typography.bodySmall)
                                            TextButton({ openDetail(id) }, enabled = !model.busy) { Text("打开记事") }
                                        }
                                        hit.score?.let { Text("相关度：$it", style = MaterialTheme.typography.bodySmall) }
                                    }
                                }
                            }
                        }
                    }
                    composable("detail") {
                        var pendingReminderAction by remember { mutableStateOf<Pair<ReminderAction, ReminderDto>?>(null) }
                        pendingReminderAction?.let { (action, reminder) ->
                            ReminderActionConfirmDialog(action, model.busy,
                                onDismiss = { pendingReminderAction = null },
                                onConfirm = {
                                    pendingReminderAction = null
                                    when (action) {
                                        ReminderAction.COMPLETE -> model.completeReminder(reminder)
                                        ReminderAction.CANCEL -> model.cancelReminder(reminder)
                                    }
                                })
                        }
                        BackHandler(enabled = model.busy) { }
                        var deleting by remember { mutableStateOf(false) }
                        val note = model.detailNote
                        if (deleting && note != null) {
                            AlertDialog(onDismissRequest = { if (!model.busy) deleting = false },
                                title = { Text("删除记事？") }, text = { Text("相关提醒也会删除，此操作不可撤销。") },
                                confirmButton = {
                                    TextButton({
                                        deleting = false
                                        model.deleteNote(note) {
                                            if (nav.currentBackStackEntry?.destination?.route == "detail") nav.popBackStack()
                                        }
                                    }, enabled = !model.busy) { Text("删除") }
                                }, dismissButton = {
                                    TextButton({ deleting = false }, enabled = !model.busy) { Text("取消") }
                                })
                        }
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            item {
                                TextButton({ model.clearDetail(); nav.popBackStack() }, enabled = !model.busy) { Text("返回") }
                            }
                            if (note != null) {
                                item {
                                    Text("标题：${note.title ?: "未整理"}", style = MaterialTheme.typography.titleMedium)
                                    Text(note.text)
                                    Text("标签：${note.tags.orEmpty().joinToString(", ").ifEmpty { "无" }}")
                                    Text("待办：")
                                    if (note.todos.isNullOrEmpty()) Text("无")
                                    note.todos.orEmpty().forEach { todo ->
                                        Text("• ${todo.text}" + (formatDisplayTime(todo.due).takeIf { it.isNotEmpty() }?.let { "（$it）" } ?: ""))
                                    }
                                    Text("分类：${note.category ?: "未分类"}")
                                    Text("状态：${noteStatus(note.status)}")
                                    Text("记录时间：${formatDisplayTimeOrUnset(note.recorded_at)}")
                                    Text("rev：${note.rev}")
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        TextButton({ model.beginEdit(note); nav.navigate("edit") }, enabled = !model.busy) { Text("编辑") }
                                        TextButton({ deleting = true }, enabled = !model.busy) { Text("删除") }
                                    }
                                }
                                val relatedReminders = remindersForNote(note.id, model.reminders)
                                if (relatedReminders.isNotEmpty()) {
                                    item { Text("关联提醒", style = MaterialTheme.typography.titleMedium) }
                                    items(relatedReminders, key = { "reminder:${it.id}" }) { reminder ->
                                        Card(Modifier.fillMaxWidth()) {
                                            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                                Text(reminder.title, style = MaterialTheme.typography.titleMedium)
                                                ReminderStatusBadge(reminder.status)
                                                Text("时间：${formatDisplayTimeOrUnset(reminder.fire_at)}")
                                                if (reminder.status in listOf("suggested", "scheduled")) {
                                                    TextButton({ pendingReminderAction = ReminderAction.CANCEL to reminder },
                                                        enabled = !model.busy) { Text("取消提醒") }
                                                    TextButton({ pendingReminderAction = ReminderAction.COMPLETE to reminder },
                                                        enabled = !model.busy) { Text("完成提醒") }
                                                }
                                                if (reminderCanSnooze(reminder.status)) {
                                                    TextButton({ model.snoozeReminder(reminder) },
                                                        enabled = !model.busy) { Text("10分钟后") }
                                                }
                                            }
                                        }
                                    }
                                }
                            } else if (!model.busy) item { Text("未能加载记事，请返回重试。") }
                        }
                    }
                    composable("edit") {
                        BackHandler(enabled = model.busy) { }
                        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            OutlinedTextField(model.editText, model::updateEdit, label = { Text("记事内容（最多 5000 字）") },
                                modifier = Modifier.fillMaxWidth().weight(1f), enabled = !model.busy)
                            Button({ model.saveEdit {
                                if (shouldPopEditRoute(nav.currentBackStackEntry?.destination?.route)) nav.popBackStack()
                            } },
                                enabled = !model.busy && model.editing != null && model.editText.isNotBlank()) { Text("保存") }
                            TextButton({ nav.popBackStack() }, enabled = !model.busy) { Text("取消") }
                        }
                    }
                    composable("capture") {
                        val onVoiceResult by rememberUpdatedState<(String) -> Unit>({ text ->
                            model.createFromVoice(text) { nav.popBackStack() }
                        })
                        val voice = remember { voiceFactory("文字记事") { onVoiceResult(it) } }
                        val captureLifecycle = LocalLifecycleOwner.current.lifecycle
                        DisposableEffect(voice, captureLifecycle) {
                            val observer = LifecycleEventObserver { _, event ->
                                if (event == Lifecycle.Event.ON_PAUSE) voice.releaseForBackground()
                            }
                            captureLifecycle.addObserver(observer)
                            onDispose { captureLifecycle.removeObserver(observer); voice.destroy() }
                        }
                        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            OutlinedTextField(model.draft, model::updateDraft, label = { Text("记事内容（最多 5000 字）") },
                                modifier = Modifier.fillMaxWidth().weight(1f), enabled = !model.busy && voice.state == AsrUiState.Idle)
                            voice.message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                            when (voice.state) {
                                AsrUiState.Idle -> OutlinedButton({ voice.start(requestMicrophone) }, enabled = !model.busy) { Text("语音记事") }
                                else -> {
                                    Text(when (voice.state) {
                                        AsrUiState.Permission -> "等待麦克风权限…"
                                        AsrUiState.Listening -> "正在听写，请说中文…"
                                        else -> "正在识别…"
                                    })
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        if (voice.state == AsrUiState.Listening) OutlinedButton(voice::stop) { Text("停止并识别") }
                                        TextButton(voice::cancel) { Text("取消") }
                                    }
                                }
                            }
                            Button({ model.save { nav.popBackStack() } }, enabled = !model.busy && voice.state == AsrUiState.Idle && model.draft.isNotBlank()) { Text("保存") }
                        }
                    }
                    composable("ask") {
                        val onVoiceResult by rememberUpdatedState<(String) -> Unit>(model::askFromVoice)
                        val voice = remember { voiceFactory("文字提问") { onVoiceResult(it) } }
                        val context = LocalContext.current
                        val speaker = remember { AnswerSpeaker(context) }
                        val askLifecycle = LocalLifecycleOwner.current.lifecycle
                        DisposableEffect(voice, speaker, askLifecycle) {
                            model.stopAnswerSpeech = speaker::stop
                            model.onAnswer = { text ->
                                if (askLifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) speaker.speak(text)
                            }
                            val observer = LifecycleEventObserver { _, event ->
                                if (event == Lifecycle.Event.ON_PAUSE) {
                                    voice.releaseForBackground()
                                    speaker.releaseForBackground()
                                }
                            }
                            askLifecycle.addObserver(observer)
                            onDispose {
                                model.onAnswer = null; model.stopAnswerSpeech = null
                                askLifecycle.removeObserver(observer); voice.destroy(); speaker.destroy()
                            }
                        }
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            item {
                                OutlinedTextField(model.question, { model.question = it }, label = { Text("问题（最多 2000 字）") },
                                    modifier = Modifier.fillMaxWidth(), enabled = !model.busy && voice.state == AsrUiState.Idle)
                                voice.message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                                when (voice.state) {
                                    AsrUiState.Idle -> OutlinedButton({ speaker.stop(); voice.start(requestMicrophone) }, enabled = !model.busy) { Text("语音提问") }
                                    else -> {
                                        Text(when (voice.state) {
                                            AsrUiState.Permission -> "等待麦克风权限…"
                                            AsrUiState.Listening -> "正在听写，请说中文…"
                                            else -> "正在识别…"
                                        })
                                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                            if (voice.state == AsrUiState.Listening) OutlinedButton(voice::stop) { Text("停止并识别") }
                                            TextButton(voice::cancel) { Text("取消") }
                                        }
                                    }
                                }
                                speaker.message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                                if (speaker.state == AnswerTtsState.Speaking) {
                                    OutlinedButton(speaker::stop) { Text("停止播报") }
                                }
                                Button(model::ask, enabled = !model.busy && voice.state == AsrUiState.Idle && model.question.isNotBlank()) { Text("提问") }
                            }
                            model.answer?.let { answer ->
                                item { Text(answer.answer) }
                                items(answer.citations) { citation ->
                                    Text("来源 ${citation.note_id?.takeIf { it.isNotBlank() } ?: "未知记事"}：${citation.text}")
                                    citation.note_id?.takeIf { it.isNotBlank() }?.let { id ->
                                        TextButton({ openDetail(id) }, enabled = !model.busy) { Text("打开记事") }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
@Composable
private fun ReminderStatusBadge(status: String?) {
    val colors = MaterialTheme.colorScheme
    val (container, content) = when (status?.trim()) {
        "suggested" -> colors.tertiaryContainer to colors.onTertiaryContainer
        "scheduled" -> colors.primaryContainer to colors.onPrimaryContainer
        "cancelled" -> colors.errorContainer to colors.onErrorContainer
        else -> colors.surfaceVariant to colors.onSurfaceVariant
    }
    Surface(color = container, contentColor = content, shape = MaterialTheme.shapes.small) {
        Text(
            reminderStatus(status),
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium
        )
    }
}

private fun noteStatus(status: String) = when (status) {
    "uploaded" -> "整理中"
    "organized" -> "已整理"
    "failed" -> "整理失败"
    else -> status
}

@Composable
private fun ConfirmReminderDialog(
    reminder: ReminderDto, busy: Boolean, title: String, fireAt: String,
    submitError: String?,
    onTitleChange: (String) -> Unit, onFireAtChange: (String) -> Unit,
    onDismiss: () -> Unit, onConfirm: (ConfirmOverrides) -> Unit
) {
    var error by remember(reminder) { mutableStateOf<String?>(null) }
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("确认并设闹钟") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(title, { onTitleChange(it); error = null }, label = { Text("标题") },
                    enabled = !busy, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(fireAt, { onFireAtChange(it); error = null }, label = { Text("提醒时间（ISO）") },
                    enabled = !busy, modifier = Modifier.fillMaxWidth())
                Text("例如 2026-11-04T12:00:00Z；留空沿用原值。时间须在未来。")
                (error ?: submitError)?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = {
            TextButton({
                try {
                    val overrides = confirmOverrides(reminder.title, reminder.fire_at, title, fireAt)
                    onConfirm(overrides)
                } catch (e: IllegalArgumentException) { error = readableError(e) }
            }, enabled = !busy) { Text("确认") }
        },
        dismissButton = { TextButton(onDismiss, enabled = !busy) { Text("取消") } }
    )
}

@Composable
private fun ReminderActionConfirmDialog(action: ReminderAction, busy: Boolean, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    val enabled = shouldAcceptReminderActionConfirm(busy)
    AlertDialog(onDismissRequest = { if (enabled) onDismiss() },
        title = { Text(reminderActionConfirmTitle(action)) },
        text = { Text(reminderActionConfirmMessage(action)) },
        confirmButton = {
            TextButton({ if (enabled) onConfirm() }, enabled = enabled) { Text("确认") }
        }, dismissButton = {
            TextButton({ if (enabled) onDismiss() }, enabled = enabled) { Text("取消") }
        })
}
