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
import androidx.compose.ui.semantics.*
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
        // Share the real session across foreground and boot work; release cannot select the mock.
        setContent {
            MaterialTheme(colorScheme = lightColorScheme(
                primary = androidx.compose.ui.graphics.Color(0xFF116B5B),
                background = androidx.compose.ui.graphics.Color(0xFFF4F5F0),
                surface = androidx.compose.ui.graphics.Color.White,
                surfaceVariant = androidx.compose.ui.graphics.Color(0xFFE9EEE8),
                onSurface = androidx.compose.ui.graphics.Color(0xFF17231F),
                outline = androidx.compose.ui.graphics.Color(0xFF74877C))) {
                val model: AgentModel = viewModel(factory = object : ViewModelProvider.Factory {
                    @Suppress("UNCHECKED_CAST")
                    override fun <T : ViewModel> create(modelClass: Class<T>): T = AgentModel(
                        if (BuildConfig.DEBUG && BuildConfig.USE_MOCK_API) MockRepository()
                        else AppRepositories.real(applicationContext),
                        ReminderAlarmScheduler(applicationContext), RoomComposerStore(applicationContext)
                    ) as T
                })
                val prompt = remember { NotificationPrompt(applicationContext) }
                var explainNotifications by remember { mutableStateOf(false) }
                if (explainNotifications) NotificationPermissionExplanation(
                    { explainNotifications = false }, {
                        explainNotifications = false
                        notificationPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)
                    })
                LaunchedEffect(model.requestNotifications) {
                    if (model.requestNotifications) {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED && prompt.claim())
                            explainNotifications = true
                        model.notifiedPermission()
                    }
                }
                AgentApp(model, { fallback, callback -> VoiceAsr(this, callback, fallback).also { voice = it } }, {
                    microphonePermission.launch(android.Manifest.permission.RECORD_AUDIO)
                }, { voice?.cancel() })
            }
        }
    }
}
class AgentModel(private val repo: Repository, private val alarms: ReminderAlarmScheduler,
    private val composerStore: ComposerStore) : ViewModel() {
    private val creates = CreateRecoveryFlow(repo, composerStore)
    private var draftVersion = 0L
    var inputMode by mutableStateOf("note"); private set
    fun setMode(mode: String) { inputMode = mode; updateDraft(draft) }
    var audioInputState by mutableStateOf(AsrUiState.Idle)
    val audioInputActive get() = audioInputState != AsrUiState.Idle
    fun beginVoice(): VoiceAttempt { audioInputState = AsrUiState.Permission; return VoiceAttempt(repo.captureOwner(), inputMode, draftVersion) }
    fun acceptVoice(attempt: VoiceAttempt, text: String) {
        audioInputState = AsrUiState.Idle
        if (!attempt.accepts(repo.isCurrent(attempt.owner), inputMode, draftVersion)) return
        updateDraft(text, if (inputMode == "note") "voice_asr" else "app")
        if (!busy) submitComposer()
    }
    fun submitComposer() {
        if (busy) return
        if (inputMode == "note") save {} else { question = draft; ask() }
    }
    var recovery by mutableStateOf<CreateRecovery?>(null); private set
    var recoveredNote by mutableStateOf<Note?>(null); private set
    private var navigationId = 0L
    private fun restoreComposer(owner: RequestOwner) {
        val state = creates.load(owner)
        draft = state.draft.text; draftVersion = state.draft.version
        draftSource = state.draft.source; inputMode = state.draft.mode; recovery = state.recovery
        if (state.draft.askOutcomeUnknown) error = "上次提问结果无法恢复；问题草稿仍保留，可手动重新提问"
    }
    var signedIn by mutableStateOf(repo.signedIn); private set
    var busy by mutableStateOf(false); private set
    var error by mutableStateOf<String?>(null); private set
    var freshness by mutableStateOf("尚未读取服务器内容；本机草稿可编辑"); private set
    var notes by mutableStateOf(emptyList<Note>()); private set
    var detailNote by mutableStateOf<Note?>(null); private set
    fun clearDetail() { navigationId++; detailNote = null }
    fun openNote(noteId: String) {
        val request = ++navigationId
        run { owner ->
            detailNote = null
            val note = repo.note(noteId, owner)
            repo.withCurrentOwner(owner) { if (request == navigationId) detailNote = note }
        }
    }
    var reminders by mutableStateOf(emptyList<ReminderDto>()); private set
    var localSchedules by mutableStateOf(emptyMap<String, LocalScheduleResult>()); private set
    var requestNotifications by mutableStateOf(false); private set
    fun notifiedPermission() { requestNotifications = false }
    private fun localResult(reminder: ReminderDto, result: LocalScheduleResult) {
        localSchedules = localSchedules + (reminder.id to result)
        if (result.submitted) requestNotifications = true
    }
    private suspend fun refreshAfterMutation(owner: RequestOwner) {
        try { refresh(owner = owner) }
        catch (e: CancellationException) { throw e }
        catch (_: Exception) { error = "服务器操作已成功；刷新失败，请手动同步" }
    }
    fun retryLocal(reminder: ReminderDto) = run { owner ->
        refreshMutex.withLock {
            val fresh = repo.sync(owner = owner).reminders.find { it.id == reminder.id }
            repo.withCurrentOwner(owner) {
                if (fresh == null || fresh.status != "scheduled") error = "提醒已变化，请同步"
                else localResult(fresh, alarms.retryLocal(fresh))
            }
        }
    }
    private var listFilterState by mutableStateOf(ReminderListFilter.ACTIVE)
    val listFilter: ReminderListFilter get() = listFilterState
    fun setListFilter(value: ReminderListFilter) { listFilterState = value }
    var answer by mutableStateOf<Answer?>(null); private set
    var answers by mutableStateOf(emptyList<AnswerItem>()); private set
    var invite by mutableStateOf("")
    var draft by mutableStateOf(""); private set
    var searchQuery by mutableStateOf("")
    var searchHits by mutableStateOf(emptyList<SearchHit>()); private set
    var searchCompleted by mutableStateOf(false); private set
    var question by mutableStateOf("")
    var organizingNoteIds by mutableStateOf(emptySet<String>()); private set
    private val organizeJobs = mutableMapOf<String, Job>()
    private val refreshMutex = Mutex()
    private val projection = NoteProjection()
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

    private var draftSource = "app"
    var onAnswer: ((String) -> Unit)? = null
    var stopAnswerSpeech: (() -> Unit)? = null
    private fun clearUi() {
        projection.clear()
        stopAnswerSpeech?.invoke()
        organizeJobs.values.toList().forEach { it.cancel() }; organizeJobs.clear()
        organizingNoteIds = emptySet()
        clearDetail()
        editing = null; editText = ""
        cancelConfirm()
        searchQuery = ""; searchHits = emptyList(); searchCompleted = false
        localSchedules = emptyMap(); requestNotifications = false; audioInputState = AsrUiState.Idle
        notes = emptyList(); reminders = emptyList(); answer = null; answers = emptyList(); draft = ""; question = ""; recovery = null; recoveredNote = null; draftSource = "app"
    }
    private fun clearSession() {
        // Fence first so in-flight boot/foreground sync cannot re-apply alarms.
        AppRepositories.bumpSession()
        alarms.clear()
        clearUi()
    }
    private fun run(login: Boolean = false, action: suspend (RequestOwner) -> Unit) {
        if (busy) { error = "上一操作尚未结束；文字已保留，请稍后手动提交"; return }
        val owner = try { if (login) RequestOwner("login", "", "", "") else repo.captureOwner() }
        catch (e: Exception) { error = readableError(e); return }
        busy = true; error = null
        viewModelScope.launch {
            try { action(owner) }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (!repo.signedIn) clearSession()
                if (repo.isCurrent(owner)) {
                    runCatching { applyRecoveryState(owner) }
                    freshness = if (notes.isEmpty()) "无法读取服务器内容；本机草稿可用" else "本次打开已加载的内容可能不是最新"
                    error = readableError(e)
                } else if (!repo.signedIn) error = readableError(e)
            }
            finally { signedIn = repo.signedIn; busy = false }
        }
    }
    private suspend fun refresh(expectedGeneration: Long? = null, owner: RequestOwner = repo.captureOwner()) = refreshMutex.withLock {
        if (!repo.isCurrent(owner)) return@withLock
        // Recheck after waiting for another refresh, before any repository/network work.
        val generation = expectedGeneration ?: AppRepositories.sessionGeneration()
        if (expectedGeneration != null &&
            (!repo.signedIn || AppRepositories.sessionGeneration() != generation)) return@withLock
        val alarmGeneration = AppRepositories.alarmGeneration()
        val ticket = projection.begin()
        val sync = repo.sync(owner = owner)
        if (!repo.signedIn || AppRepositories.sessionGeneration() != generation) {
            if (!repo.signedIn) clearSession()
            return@withLock
        }
        val fresh = sync.notes.sortedByDescending { it.recorded_at }
        repo.withCurrentOwner(owner) {
        sync.deleted.notes.forEach { creates.markDeleted(owner, it) }
        recovery = creates.load(owner).recovery
        AppRepositories.consumeIfCurrent(generation, alarmGeneration) {
            if (repo.signedIn) {
                localSchedules = localSchedules + alarms.consume(sync)
                reminders = sync.reminders.sortedWith(compareBy(nullsLast()) { it.fire_at })
            }
        }
        if (repo.signedIn && AppRepositories.sessionGeneration() == generation) {
            notes = projection.apply(ticket, fresh, notes)
            sync.deleted.notes.forEach { applyMissingNote(owner, it) }
            recoveredNote = recoveredNote?.let { old -> notes.find { it.id == old.id } }
            detailNote = syncedDetailNote(detailNote, notes)
            freshness = "本次打开已同步；整理完成后，记忆仍可能稍后才能搜到"
        }
    }
    }
    fun login() = run(login = true) { repo.login(invite); AppRepositories.bumpSession(); invite = ""; restoreComposer(repo.captureOwner()) }
    fun reload() = run { owner -> restoreComposer(owner); refresh(owner = owner) }
    fun confirmReminder(reminder: ReminderDto, overrides: ConfirmOverrides) = run { owner ->
        require(java.time.Instant.parse(overrides.fireAt ?: reminder.fire_at).isAfter(java.time.Instant.now())) { "时间已过，请修改为未来时间" }
        refreshMutex.withLock {
        confirmReminderSteps(
            confirm = { repo.confirmReminder(reminder.id, reminder.rev, overrides.fireAt, overrides.title, owner) },
            schedule = { response -> repo.withCurrentOwner(owner) { alarms.confirmed(response) } },
            apply = { confirmed, local -> repo.withCurrentOwner(owner) {
                cancelConfirm()
                reminders = listOf(confirmed) + reminders.filterNot { it.id == reminder.id }
                localResult(confirmed, local)
            } },
            refresh = { })
        }
        refreshAfterMutation(owner)
    }
    private fun applyReminderMutation(owner: RequestOwner, reminder: ReminderDto,
        schedule: (() -> LocalScheduleResult)? = null) = repo.withCurrentOwner(owner) {
        reminders = listOf(reminder) + reminders.filterNot { it.id == reminder.id }
        AppRepositories.invalidateAlarms(reminder.id) {
            val result = try {
                alarms.cancelReminder(reminder.id)
                schedule?.invoke() ?: LocalScheduleResult.NOT_APPLICABLE
            } catch (_: Exception) { LocalScheduleResult.FAILED }
            localResult(reminder, result)
            if (result == LocalScheduleResult.FAILED) error = "服务端操作已成功；本机闹钟处理失败，请同步核对"
        }
    }
    fun cancelReminder(reminder: ReminderDto) = run { owner ->
        refreshMutex.withLock {
            applyReminderMutation(owner, repo.cancelReminder(reminder.id, reminder.rev, owner).reminder)
        }
        refreshAfterMutation(owner)
    }
    fun completeReminder(reminder: ReminderDto) = run { owner ->
        refreshMutex.withLock {
            applyReminderMutation(owner, repo.completeReminder(reminder.id, reminder.rev, owner).reminder)
        }
        refreshAfterMutation(owner)
    }
    fun snoozeReminder(reminder: ReminderDto) = run { owner ->
        refreshMutex.withLock {
            val response = repo.snoozeReminder(reminder.id, reminder.rev, minutes = 10, owner = owner)
            applyReminderMutation(owner, response.reminder) { alarms.snoozed(response) }
        }
        refreshAfterMutation(owner)
    }
    fun updateDraft(value: String) = updateDraft(value, "app")
    private fun updateDraft(value: String, source: String) {
        draft = value; draftSource = source; draftVersion++
        try {
            val owner = repo.captureOwner()
            creates.edit(owner, ComposerDraft(draft, draftVersion, mode = inputMode, source = draftSource))
        } catch (_: Exception) { error = "无法保存本机草稿；文字仍在，尚未发送" }
    }
    fun createFromVoice(text: String, done: () -> Unit) {
        if (busy) return
        updateDraft(text)
        draftSource = "voice_asr"
        save(done)
    }
    fun save(done: () -> Unit) = run { owner ->
        validText(draft, 5000)
        creates.edit(owner, ComposerDraft(draft, draftVersion, mode = inputMode, source = draftSource))
        val original = creates.prepare(owner) // disk failure throws before HTTP
        restoreComposer(owner)
        refreshMutex.withLock {
            val note = creates.send(owner, original)
            applyCreated(owner, note)
        }
        done()
    }
    private fun applyCreated(owner: RequestOwner, note: Note) = repo.withCurrentOwner(owner) {
        recoveredNote = note
        restoreComposer(owner) // B is read back, never consumed by A's response
        notes = projection.upsert(note, notes)
        waitForOrganize(Created(note.id, note.status, note.rev), owner)
    }
    private fun applyMissingNote(owner: RequestOwner, id: String) = repo.withCurrentOwner(owner) {
        notes = projection.delete(id, notes)
        if (recoveredNote?.id == id) recoveredNote = null
        if (detailNote?.id == id) clearDetail()
        val related = reminders.filter { it.note_id == id }.map { it.id }
        reminders = reminders.filterNot { it.note_id == id }
        AppRepositories.invalidateAlarms {
            alarms.consume(SyncResponse(0, deleted = SyncDeleted(notes = listOf(id), reminders = related)))
        }
    }
    private fun applyRecoveryState(owner: RequestOwner) = repo.withCurrentOwner(owner) {
        recovery = creates.load(owner).recovery
        if (recovery?.state == "deleted") recovery?.discoveredNoteId?.let { applyMissingNote(owner, it) }
    }
    private suspend fun reconcileUnderLock(owner: RequestOwner, retry: Boolean = false): Note? {
        try {
            val note = if (retry) creates.retryIdentical(owner) else creates.reconcile(owner)
            if (note != null) applyCreated(owner, note)
            return note
        } finally { if (repo.isCurrent(owner)) applyRecoveryState(owner) }
    }
    fun reconcileCreate(retry: Boolean = false) = run { owner ->
        refreshMutex.withLock {
            val note = reconcileUnderLock(owner, retry)
            if (note == null) error = if (recovery?.state == "deleted") "原记事已删除；草稿保留，请明确移除恢复项后另存" else
                "尚未找到原记事；可明确原样重试 A，或移除本机恢复项"
        }
    }
    fun removeRecovery() = run { owner ->
        creates.removeLocalRecovery(owner); restoreComposer(owner); recoveredNote = null
    }
    fun saveCopy() = run { owner ->
        refreshMutex.withLock {
            check(recovery?.state == "live" && recoveredNote?.id == recovery?.discoveredNoteId) { "请先核对原记事" }
            creates.edit(owner, ComposerDraft(draft, draftVersion, mode = "note", source = draftSource))
            val original = creates.prepare(owner, replaceLive = true)
            recoveredNote = null
            restoreComposer(owner)
            try { applyCreated(owner, creates.send(owner, original)) }
            finally { if (repo.isCurrent(owner)) applyRecoveryState(owner) }
        }
    }
    fun updateRecovered() = run { owner ->
        refreshMutex.withLock {
            val original = recoveredNote?.takeIf { recovery?.state == "live" } ?: return@withLock
            val version = draftVersion
            try {
                val updated = repo.patch(original.id, original.rev, draft, owner)
                repo.withCurrentOwner(owner) {
                    creates.removeLocalRecovery(owner)
                    if (version == draftVersion) creates.edit(owner, ComposerDraft(version = version + 1))
                    applyCreated(owner, updated)
                }
            } catch (e: retrofit2.HttpException) {
                when (e.code()) {
                    409 -> {
                        reconcileUnderLock(owner)
                        error = "已加载服务器最新内容；草稿保留，请再次明确选择更新或另存"
                    }
                    404 -> {
                        creates.markDeleted(owner, original.id)
                        applyRecoveryState(owner)
                        error = "原记事已删除；草稿仍保留"
                    }
                    else -> throw e
                }
            }
        }
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
    fun saveEdit(done: () -> Unit) = run { owner ->
        val original = editing ?: return@run
        try {
            val updated = refreshMutex.withLock {
                repo.patch(original.id, original.rev, editText, owner).also { note ->
                    repo.withCurrentOwner(owner) {
                        notes = projection.upsert(note, notes)
                        if (detailNote?.id == note.id) detailNote = note
                    }
                }
            }
            editing = null; editText = ""
            done()
            if (shouldPollOrganizeAfterEdit(updated.status)) {
                organizeJobs[updated.id]?.cancelAndJoin()
                waitForOrganize(Created(updated.id, updated.status, updated.rev), owner)
            }
            refresh(owner = owner)
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if ((e is retrofit2.HttpException && e.code() == 409) ||
                (e is IllegalArgumentException && e.message?.contains("版本冲突") == true)) {
                error = readableError(e)
                refresh(owner = owner)
                editing = notes.find { it.id == original.id }
            } else throw e
        }
    }
    fun deleteNote(note: Note, done: () -> Unit = {}) = run { owner ->
        refreshMutex.withLock {
            repo.delete(note.id, owner)
            creates.markDeleted(owner, note.id)
            recovery = creates.load(owner).recovery
            if (recoveredNote?.id == note.id) recoveredNote = null
            AppRepositories.invalidateAlarms {
                alarms.consume(SyncResponse(0, deleted = SyncDeleted(
                    notes = listOf(note.id),
                    reminders = reminders.filter { it.note_id == note.id }.map { it.id })))
            }
            reminders = reminders.filterNot { it.note_id == note.id }
            organizeJobs.remove(note.id)?.cancel()
            organizingNoteIds = organizingNoteIds - note.id
            notes = projection.delete(note.id, notes)
            if (detailNote?.id == note.id) clearDetail()
        }
        done()
        refreshAfterMutation(owner)
    }
    private fun waitForOrganize(created: Created, owner: RequestOwner) {
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
                            val ticket = projection.begin()
                            val fresh = repo.notes(owner)
                            if (!repo.signedIn || AppRepositories.sessionGeneration() != generation) return@withTimeoutOrNull
                            repo.withCurrentOwner(owner) {
                                notes = projection.apply(ticket, fresh, notes)
                                detailNote = syncedDetailNote(detailNote, notes)
                                status = notes.find { it.id == id }?.status
                            }
                        }
                        attempt++
                    }
                }
                requireSessionAfterOrganizePoll(AppRepositories.sessionGeneration() == generation, repo.signedIn)
                if (repo.isCurrent(owner) && AppRepositories.sessionGeneration() == generation) refresh(owner = owner)
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (AppRepositories.sessionGeneration() == generation) {
                    if (!repo.signedIn) { clearSession(); signedIn = false }
                    error = readableError(e)
                }
            } finally {
                if (repo.isCurrent(owner)) {
                    organizingNoteIds = organizingNoteIds - id
                    organizeJobs.remove(id)
                }
            }
        }
    }
    fun askFromVoice(text: String) {
        val result = voiceQuestion(text, busy)
        question = result.question
        if (result.shouldAutoAsk) ask()
    }
    fun search() = run { owner ->
        searchCompleted = false; searchHits = emptyList()
        searchHits = repo.search(searchQuery, owner = owner); searchCompleted = true
    }
    fun askAgain(text: String) { if (!busy) { question = text; ask() } }
    fun ask() = run { owner ->
        stopAnswerSpeech?.invoke()
        val asked = question
        val submittedVersion = draftVersion
        validText(asked, 2000)
        val item = AnswerItem(asked)
        answers = listOf(item) + answers
        answer = null
        try {
            creates.edit(owner, ComposerDraft(draft, draftVersion, inputMode, askOutcomeUnknown = true))
            val result = repo.ask(asked, owner)
            repo.withCurrentOwner(owner) {
                answer = result
                answers = answers.map { if (it.id == item.id) item.copy(answer = result) else it }
                val current = creates.load(owner).draft
                creates.edit(owner, if (current.version == submittedVersion && current.mode == "ask" && current.text == asked)
                    current.copy(text = "", version = current.version + 1, askOutcomeUnknown = false)
                    else current.copy(askOutcomeUnknown = false))
                restoreComposer(owner)
            }
        } catch (e: Exception) {
            if (repo.isCurrent(owner)) answers = answers.map { if (it.id == item.id) item.copy(failed = true) else it }
            throw e
        }
    }
    fun logout() {
        if (busy) return
        val session = try { repo.beginLogout(repo.captureOwner()) }
            catch (e: Exception) { error = readableError(e); return }
        // Persistent epoch/closed/process fence is already committed. Nothing above cleans local data.
        AppRepositories.bumpSession()
        signedIn = false; busy = true
        viewModelScope.launch {
            try {
                repo.withClosingSession(session) {
                    stopAnswerSpeech?.invoke()
                    try { composerStore.clear() }
                    finally { try { alarms.clear() } finally { clearUi() } }
                }
            } catch (e: Exception) { error = readableError(e) }
            finally {
                try { withTimeoutOrNull(8_000L) { repo.finishLogout(session) } }
                catch (e: Exception) { if (e is CancellationException) throw e }
                finally { busy = false; signedIn = repo.signedIn }
            }
        }
    }

}
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AgentApp(model: AgentModel, voiceFactory: (String, (String) -> Unit) -> VoiceAsr, requestMicrophone: () -> Unit, cancelVoice: () -> Unit) {
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize().imePadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("随记" + if (BuildConfig.USE_MOCK_API) " / 本地演示" else " / 服务器", style = MaterialTheme.typography.headlineSmall)
            if (model.signedIn) Text(model.freshness, style = MaterialTheme.typography.bodySmall)
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            model.error?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
            if (!model.signedIn) {
                OutlinedTextField(model.invite, { model.invite = it }, label = { Text("邀请码") },
                    enabled = !model.busy, modifier = Modifier.fillMaxWidth(), singleLine = true)
                Text(if (BuildConfig.USE_MOCK_API) "演示模式：输入任意非空邀请码。记事与提醒仅保留在内存中。" else "请输入服务端签发的邀请码。")
                Button(model::login, enabled = !model.busy && model.invite.isNotBlank(), modifier = Modifier.touchTarget()) { Text("登录") }
            } else {
                val context = LocalContext.current
                val speaker = remember { AnswerSpeaker(context) }
                val lifecycle = LocalLifecycleOwner.current.lifecycle
                DisposableEffect(speaker, lifecycle) {
                    model.stopAnswerSpeech = speaker::stop
                    model.onAnswer = null // Manual replay only; delayed answers never interrupt listening.
                    val observer = LifecycleEventObserver { _, event ->
                        if (event == Lifecycle.Event.ON_PAUSE) speaker.releaseForBackground()
                    }
                    lifecycle.addObserver(observer)
                    onDispose { model.stopAnswerSpeech = null; lifecycle.removeObserver(observer); speaker.destroy() }
                }
                val nav = rememberNavController()
                val openDetail: (String) -> Unit = { id -> model.openNote(id); nav.navigate("detail") }
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton({ cancelVoice(); nav.navigate("timeline") { popUpTo("timeline") { inclusive = true } } }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("个人流") }
                    TextButton({ cancelVoice(); nav.navigate("status") }, modifier = Modifier.touchTarget()) { Text("同步与权限") }
                    TextButton({ cancelVoice(); model.logout() }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("退出") }
                }
                model.confirming?.let { reminder ->
                    ReminderConfirmationDialog(reminder, model.busy,
                        title = model.confirmTitle, fireAt = model.confirmFireAt,
                        submitError = model.error,
                        onTitleChange = model::updateConfirmTitle,
                        onFireAtChange = model::updateConfirmFireAt,
                        onDismiss = model::cancelConfirm,
                        onConfirm = { overrides -> model.confirmReminder(reminder, overrides) })
                }
                NavHost(nav, startDestination = "timeline", modifier = Modifier.weight(1f)) {
                    composable("timeline") {
                        var pendingReminderAction by remember { mutableStateOf<Pair<ReminderAction, ReminderDto>?>(null) }
                        pendingReminderAction?.let { (action, reminder) ->
                            ReminderActionConfirmDialog(action, reminder.title, model.busy,
                                onDismiss = { pendingReminderAction = null },
                                onConfirm = {
                                    pendingReminderAction = null
                                    when (action) {
                                        ReminderAction.COMPLETE -> model.completeReminder(reminder)
                                        ReminderAction.CANCEL -> model.cancelReminder(reminder)
                                    }
                                })
                        }
                        var flowFilter by remember { mutableStateOf("全部") }
                        val visibleReminders = if (flowFilter == "提醒") filterRemindersForList(model.reminders, model.listFilter)
                            else if (flowFilter == "全部") model.reminders.filter { it.note_id == null || model.notes.none { n -> n.id == it.note_id } }
                            else emptyList()
                        var deleting by remember { mutableStateOf<Note?>(null) }
                        deleting?.let { note ->
                            AlertDialog(onDismissRequest = { if (!model.busy) deleting = null },
                                title = { Text("删除记事？") }, text = { Text("相关提醒也会删除，此操作不可撤销。") },
                                confirmButton = {
                                    TextButton({ deleting = null; model.deleteNote(note) }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("删除") }
                                }, dismissButton = {
                                    TextButton({ deleting = null }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("取消") }
                                })
                        }
                        LaunchedEffect(Unit) { model.reload() }
                        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Text("把想法放下，其他慢慢来。", style = MaterialTheme.typography.headlineSmall)
                            TextButton({ nav.navigate("search") }, modifier = Modifier.touchTarget()) { Text("搜索记忆") }
                            TextButton(model::reload, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("同步") }
                            Row(Modifier.fillMaxWidth()) {
                                listOf("全部", "记事", "问答", "提醒").forEach { filter ->
                                    Tab(modifier = Modifier.touchTarget().weight(1f), selected = flowFilter == filter,
                                        onClick = { flowFilter = filter }, text = { Text(filter) })
                                }
                            }
                            LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                item {
                                    model.reminders.firstOrNull { it.status == "suggested" && it.note_id != null }?.let { suggestion ->
                                        OutlinedButton({ model.beginConfirm(suggestion) }, enabled = !model.busy,
                                            modifier = Modifier.touchTarget().fillMaxWidth()) { Text("有提醒等你确认：${suggestion.title}") }
                                    }
                                    model.recovery?.let { recovery ->
                                        RecoveryCard(recovery, model.recoveredNote, model.draft, model.busy,
                                            { model.reconcileCreate() }, { model.reconcileCreate(true) },
                                            model::updateRecovered, model::saveCopy, model::removeRecovery)
                                    }
                                }
                                items(if (flowFilter in listOf("全部", "问答")) model.answers else emptyList(), key = { "answer:${it.id}" }) { item ->
                                    AnswerCard(item, model.busy, model.audioInputState, speaker.state,
                                        speaker::speak, speaker::stop, { model.askAgain(item.question) }, openDetail,
                                        { nav.navigate("search") }, speaker.message)
                                }
                                if (flowFilter == "提醒") item {
                                    FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                        Text("提醒", style = MaterialTheme.typography.titleMedium)
                                        ReminderListFilter.entries.forEach { filter ->
                                            FilterChip(selected = model.listFilter == filter,
                                                onClick = { model.setListFilter(filter) },
                                                label = { Text(filter.label) }, modifier = Modifier.touchTarget())
                                        }
                                    }
                                }
                                if (flowFilter == "提醒" && visibleReminders.isEmpty() && !model.busy) item { Text(reminderListEmptyMessage(model.reminders, model.listFilter)) }
                                items(visibleReminders, key = { "reminder:${it.id}" }) { reminder ->
                                    FlowReminderCard(model, reminder, openDetail) { action, item -> pendingReminderAction = action to item }
                                }
                                if (flowFilter in listOf("全部", "记事")) item { Text("记事", style = MaterialTheme.typography.titleMedium) }
                                if (flowFilter in listOf("全部", "记事") && model.notes.isEmpty() && !model.busy) item { Text("还没有记事，试着记录第一条。") }
                                items(if (flowFilter in listOf("全部", "记事")) model.notes else emptyList(), key = { "note:${it.id}" }) { note ->
                                    Card(Modifier.fillMaxWidth()) {
                                        Column(Modifier.padding(12.dp)) {
                                            note.title?.let { Text(it, style = MaterialTheme.typography.titleMedium) }
                                            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                                TextButton({ openDetail(note.id) }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("详情") }
                                                TextButton({ model.beginEdit(note); nav.navigate("edit") }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("编辑") }
                                                TextButton({ deleting = note }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("删除") }
                                            }
                                            Text(note.text, maxLines = 4, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                                            remindersForNote(note.id, model.reminders).forEach { reminder ->
                                                FlowReminderCard(model, reminder, openDetail) { action, item -> pendingReminderAction = action to item }
                                            }
                                            if (note.status == "uploaded") {
                                                Text(if (note.id in model.organizingNoteIds) "已保存 · 正在整理" else "已保存 · 整理尚未完成", style = MaterialTheme.typography.bodySmall,
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
                                item { UnifiedComposer(model, voiceFactory, requestMicrophone) }
                            }
                        }
                    }
                    composable("status") { SystemStatus(model) }
                    composable("search") {
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            item {
                                OutlinedTextField(model.searchQuery, { model.searchQuery = it },
                                    label = { Text("搜索记忆（最多 500 字）") },
                                    modifier = Modifier.fillMaxWidth(), enabled = !model.busy)
                                Button(model::search, enabled = !model.busy && model.searchQuery.isNotBlank(), modifier = Modifier.touchTarget()) { Text("搜索") }
                            }
                            if (model.searchCompleted && model.searchHits.isEmpty() && !model.busy) item { Text("没有找到相关记忆，可以试试更短的关键词。") }
                            items(model.searchHits) { hit ->
                                Card(Modifier.fillMaxWidth()) {
                                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                        Text(hit.text)
                                        hit.note_id?.takeIf { it.isNotBlank() }?.let { id ->
                                            TextButton({ openDetail(id) }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("打开记事") }
                                        }
                                        if (hit.note_id.isNullOrBlank()) Text("来源记事不可用")
                                    }
                                }
                            }
                        }
                    }
                    composable("detail") {
                        var pendingReminderAction by remember { mutableStateOf<Pair<ReminderAction, ReminderDto>?>(null) }
                        pendingReminderAction?.let { (action, reminder) ->
                            ReminderActionConfirmDialog(action, reminder.title, model.busy,
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
                                    }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("删除") }
                                }, dismissButton = {
                                    TextButton({ deleting = false }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("取消") }
                                })
                        }
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            item {
                                TextButton({ model.clearDetail(); nav.popBackStack() }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("返回") }
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
                                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        TextButton({ model.beginEdit(note); nav.navigate("edit") }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("编辑") }
                                        TextButton({ deleting = true }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("删除") }
                                    }
                                }
                                val relatedReminders = remindersForNote(note.id, model.reminders)
                                if (relatedReminders.isNotEmpty()) {
                                    item { Text("关联提醒", style = MaterialTheme.typography.titleMedium) }
                                    items(relatedReminders, key = { "reminder:${it.id}" }) { reminder ->
                                        FlowReminderCard(model, reminder, openDetail) { action, item -> pendingReminderAction = action to item }
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
                                enabled = !model.busy && model.editing != null && model.editText.isNotBlank(), modifier = Modifier.touchTarget()) { Text("保存") }
                            TextButton({ nav.popBackStack() }, enabled = !model.busy, modifier = Modifier.touchTarget()) { Text("取消") }
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
