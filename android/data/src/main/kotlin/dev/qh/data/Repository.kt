package dev.qh.data

import com.google.gson.Gson
import java.io.IOException
import java.time.Instant
import java.time.ZoneId
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.OkHttpClient
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

interface TokenStore {
    fun read(): Tokens?
    fun write(tokens: Tokens?)
    fun identity(): SessionIdentity?
    /** Atomically persist a new login epoch and closed=true before returning. */
    fun closeSession()
    fun bumpLoginEpoch()
    fun deviceId(): String
}
class SessionExpired : IOException("会话已失效，请重新登录")
interface Repository {
    fun captureOwner(): RequestOwner
    fun isCurrent(owner: RequestOwner): Boolean
    fun <T> withCurrentOwner(owner: RequestOwner, block: () -> T): T
    suspend fun snoozeReminder(id: String, rev: Int, minutes: Int = 10, owner: RequestOwner = captureOwner()): ReminderSnoozeResponse
    suspend fun cancelReminder(id: String, rev: Int, owner: RequestOwner = captureOwner()): ReminderCancelResponse
    suspend fun completeReminder(id: String, rev: Int, owner: RequestOwner = captureOwner()): ReminderCompleteResponse
    val signedIn: Boolean
    suspend fun login(invite: String)
    fun beginLogout(owner: RequestOwner): LogoutSession
    fun withClosingSession(session: LogoutSession, block: () -> Unit)
    suspend fun finishLogout(session: LogoutSession)
    suspend fun logout(owner: RequestOwner = captureOwner()) { finishLogout(beginLogout(owner)) }
    fun validatedOwner(validate: () -> Unit): RequestOwner { validate(); return captureOwner() }
    suspend fun note(noteId: String, owner: RequestOwner = validatedOwner { require(noteId.isNotBlank()) { "记事 ID 不能为空" } }): Note
    suspend fun notes(owner: RequestOwner = captureOwner()): List<Note>
    suspend fun create(body: NoteCreate, owner: RequestOwner = validatedOwner { validText(body.text, 5000) }): Created
    suspend fun patch(noteId: String, rev: Int, text: String?, owner: RequestOwner = validatedOwner { if (text != null) validText(text, 5000) }): Note
    suspend fun delete(noteId: String, owner: RequestOwner = captureOwner())
    suspend fun search(query: String, topK: Int = 10, owner: RequestOwner = validatedOwner { validText(query.trim(), 500); require(topK in 1..50) { "搜索条数须为 1–50" } }): List<SearchHit>
    suspend fun ask(text: String, owner: RequestOwner = validatedOwner { validText(text, 2000) }): Answer
    suspend fun sync(since: Long = 0, limit: Int = 100, owner: RequestOwner = validatedOwner { require(since >= 0 && limit in 1..500) { "同步参数无效" } }): SyncResponse
    suspend fun confirmReminder(id: String, rev: Int, fireAt: String? = null,
        title: String? = null, owner: RequestOwner = captureOwner()): ReminderConfirmResponse
}
fun noteDraft(text: String, id: String = UUID.randomUUID().toString(), source: String = "app") = NoteCreate(
    id, text.trim(), Instant.now().toString(), ZoneId.systemDefault().id, source)
fun askBody(text: String) = AskRequest(text.trim(), Instant.now().toString(), ZoneId.systemDefault().id)
fun validText(text: String, limit: Int) {
    require(text.isNotBlank() && text.codePointCount(0, text.length) <= limit && '\u0000' !in text) {
        "请输入有效内容（最多 $limit 字）"
    }
}
fun readableError(error: Throwable): String = when (error) {
    is SessionExpired -> "会话已失效，请重新登录"
    is HttpException -> {
        val body = runCatching { Gson().fromJson(error.response()?.errorBody()?.string(), ErrorEnvelope::class.java) }.getOrNull()
        when (error.code()) {
            401 -> "登录已失效或邀请码无效，请重新登录"
            404 -> body?.error?.message?.take(240) ?: "记事不存在（404）"
            409 -> "版本冲突，请同步后重试"
            422 -> "输入不符合要求，请检查长度和格式"
            else -> body?.error?.message?.take(240) ?: "服务请求失败（HTTP ${error.code()}），请重试"
        }
    }
    is IllegalArgumentException -> error.message ?: "输入无效"
    is IOException -> "网络连接失败，请检查服务器地址和网络后重试"
    else -> "操作失败，请稍后重试"
}
fun realApi(baseUrl: String): AgentApi = Retrofit.Builder().baseUrl(baseUrl)
    .client(OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS).callTimeout(75, TimeUnit.SECONDS).build())
    .addConverterFactory(GsonConverterFactory.create()).build().create(AgentApi::class.java)

object SessionMutex {
    val lock = Mutex()
}

class RealRepository(private val api: AgentApi, private val store: TokenStore,
    private val mutex: Mutex = SessionMutex.lock,
    private val runtimeKey: String = "real") : Repository {
    private val sessionLock = Any()
    private var processGeneration = UUID.randomUUID().toString()
    @Volatile private var closing = false
    override fun captureOwner(): RequestOwner = synchronized(sessionLock) {
        if (closing) throw StaleRequestOwner()
        val identity = store.identity()?.takeUnless { it.closed } ?: throw SessionExpired()
        RequestOwner(runtimeKey, identity.userId, identity.loginEpoch, processGeneration)
    }
    override fun isCurrent(owner: RequestOwner): Boolean = synchronized(sessionLock) {
        val identity = store.identity()
        !closing && identity?.closed == false && owner.runtimeKey == runtimeKey && owner.processGeneration == processGeneration &&
            identity?.userId == owner.userId && identity.loginEpoch == owner.loginEpoch
    }
    override fun <T> withCurrentOwner(owner: RequestOwner, block: () -> T): T = synchronized(sessionLock) {
        requireCurrent(owner)
        block()
    }
    private fun requireCurrent(owner: RequestOwner) { if (!isCurrent(owner)) throw StaleRequestOwner() }
    private fun clearIfCurrent(owner: RequestOwner) = synchronized(sessionLock) {
        if (isCurrent(owner)) { store.bumpLoginEpoch(); store.write(null) }
    }
    // Serialize session operations process-wide: a rotated refresh token must never be replayed concurrently.
    override val signedIn get() = !closing && store.identity()?.closed == false
    override suspend fun login(invite: String) {
        validText(invite.trim(), 64)
        val attempt = synchronized(sessionLock) {
            closing = true
            store.bumpLoginEpoch()
            store.write(null)
            UUID.randomUUID().toString().also { processGeneration = it }
        }
        mutex.withLock {
            synchronized(sessionLock) { if (processGeneration != attempt) throw StaleRequestOwner() }
            val tokens = api.login(LoginRequest(invite.trim(), store.deviceId()))
            synchronized(sessionLock) {
                if (processGeneration != attempt) throw StaleRequestOwner()
                store.bumpLoginEpoch()
                store.write(tokens)
                closing = false
            }
        }
    }
    private suspend fun <T> authorized(owner: RequestOwner, block: suspend (String) -> T): T = mutex.withLock {
        val tokens = synchronized(sessionLock) {
            requireCurrent(owner) // Must precede credential read, including after mutex waiting.
            store.read() ?: throw SessionExpired()
        }
        val result = try { block("Bearer ${tokens.access_token}") }
        catch (error: HttpException) {
            if (error.code() != 401) throw error
            requireCurrent(owner)
            val fresh = try { api.refresh(RefreshRequest(tokens.refresh_token)) }
            catch (error: CancellationException) { clearIfCurrent(owner); throw error }
            catch (error: Exception) { clearIfCurrent(owner); throw SessionExpired() }
            synchronized(sessionLock) {
                requireCurrent(owner)
                if (fresh.user_id != owner.userId) { clearIfCurrent(owner); throw SessionExpired() }
                store.write(fresh) // Ordinary refresh retains the login epoch.
            }
            requireCurrent(owner)
            try { block("Bearer ${fresh.access_token}") }
            catch (error: HttpException) {
                if (error.code() == 401) { clearIfCurrent(owner); throw SessionExpired() }
                throw error
            }
        }
        requireCurrent(owner)
        result
    }
    override fun beginLogout(owner: RequestOwner): LogoutSession = synchronized(sessionLock) {
        requireCurrent(owner)
        store.closeSession()
        closing = true
        processGeneration = UUID.randomUUID().toString()
        LogoutSession(processGeneration, store.identity())
    }
    private fun stillClosing(session: LogoutSession) = closing &&
        processGeneration == session.generation && store.identity() == session.identity
    override fun withClosingSession(session: LogoutSession, block: () -> Unit) = synchronized(sessionLock) {
        if (!stillClosing(session)) throw StaleRequestOwner()
        block()
    }
    override suspend fun finishLogout(session: LogoutSession): Unit = mutex.withLock {
        val tokens = synchronized(sessionLock) {
            if (!stillClosing(session)) throw StaleRequestOwner()
            store.read()
        }
        try { tokens?.let { api.logout("Bearer ${it.access_token}") } }
        finally { synchronized(sessionLock) {
            if (stillClosing(session)) { store.write(null); closing = false }
        } }
        Unit
    }
    override suspend fun note(noteId: String, owner: RequestOwner): Note {
        require(noteId.isNotBlank()) { "记事 ID 不能为空" }
        return authorized(owner) { api.getNote(it, noteId) }
    }
    override suspend fun notes(owner: RequestOwner): List<Note> = authorized(owner) { bearer ->
        val result = mutableListOf<Note>()
        var cursor = 0L
        do {
            requireCurrent(owner)
            val page = api.notes(bearer, cursor)
            result.addAll(page.items)
            val next = page.next_cursor ?: break
            check(next > cursor) { "Invalid pagination" }
            cursor = next
        } while (true)
        result.distinctBy { it.id }.reversed()
    }
    override suspend fun create(body: NoteCreate, owner: RequestOwner): Created {
        validText(body.text, 5000)
        return authorized(owner) { api.create(it, body) }
    }
    override suspend fun patch(noteId: String, rev: Int, text: String?, owner: RequestOwner): Note {
        if (text != null) validText(text, 5000)
        return authorized(owner) { api.patchNote(it, noteId, NotePatch(rev, text)).note }
    }
    override suspend fun delete(noteId: String, owner: RequestOwner) {
        try { authorized(owner) { api.deleteNote(it, noteId) } }
        catch (error: HttpException) { if (error.code() != 404) throw error }
    }
    override suspend fun search(query: String, topK: Int, owner: RequestOwner): List<SearchHit> {
        val q = query.trim(); validText(q, 500)
        require(topK in 1..50) { "搜索条数须为 1–50" }
        return authorized(owner) { api.search(it, q, topK).items }
    }
    override suspend fun ask(text: String, owner: RequestOwner): Answer {
        validText(text, 2000)
        return authorized(owner) { api.ask(it, askBody(text)) }
    }
    override suspend fun sync(since: Long, limit: Int, owner: RequestOwner): SyncResponse {
        require(since >= 0 && limit in 1..500) { "同步参数无效" }
        return authorized(owner) { bearer ->
            val notes = linkedMapOf<String, Note>()
            val reminders = linkedMapOf<String, ReminderDto>()
            val actions = linkedMapOf<String, SyncAction>()
            val deletedNotes = linkedSetOf<String>()
            val deletedReminders = linkedSetOf<String>()
            val deletedActions = linkedSetOf<String>()
            var cursor = since
            while (true) {
                requireCurrent(owner)
                val page = api.sync(bearer, cursor, limit)
                page.notes.forEach { notes[it.id] = it }
                page.reminders.forEach { reminders[it.id] = it }
                page.actions.forEach { actions[it.id] = it }
                page.deleted.notes.forEach { notes.remove(it); deletedNotes += it }
                page.deleted.reminders.forEach { reminders.remove(it); deletedReminders += it }
                page.deleted.actions.forEach { actions.remove(it); deletedActions += it }
                if (!page.has_more) {
                    cursor = page.cursor
                    break
                }
                check(page.cursor > cursor) { "Invalid pagination" }
                cursor = page.cursor
            }
            SyncResponse(cursor, notes.values.toList(), reminders.values.toList(), actions.values.toList(),
                SyncDeleted(deletedNotes.toList(), deletedReminders.toList(), deletedActions.toList()), has_more = false)
        }
    }
    override suspend fun confirmReminder(id: String, rev: Int, fireAt: String?, title: String?, owner: RequestOwner) =
        authorized(owner) { api.confirmReminder(it, id, ReminderConfirmRequest(rev, fireAt, title)) }
    override suspend fun cancelReminder(id: String, rev: Int, owner: RequestOwner) =
        authorized(owner) { api.cancelReminder(it, id, ReminderCancelRequest(rev)) }
    override suspend fun completeReminder(id: String, rev: Int, owner: RequestOwner) =
        authorized(owner) { api.completeReminder(it, id, ReminderCompleteRequest(rev)) }
    override suspend fun snoozeReminder(id: String, rev: Int, minutes: Int, owner: RequestOwner) =
        authorized(owner) { api.snoozeReminder(it, id, ReminderSnoozeRequest(rev, minutes)) }

}
class MockRepository(private val seedOnLogin: Boolean = true, private val organizeAfterReads: Int = 3) : Repository {
    private data class Change(val seq: Long, val kind: String, val id: String)
    private val organizeReads = mutableMapOf<String, Int>()
    private var mockEpoch = UUID.randomUUID().toString()
    override fun captureOwner() = RequestOwner("mock", "demo", mockEpoch, mockEpoch)
    override fun isCurrent(owner: RequestOwner) = active && owner.runtimeKey == "mock" && owner.loginEpoch == mockEpoch
    override fun <T> withCurrentOwner(owner: RequestOwner, block: () -> T): T {
        if (!isCurrent(owner)) throw StaleRequestOwner()
        return block()
    }
    private var active = false
    private var cursor = 0L
    private val memories = linkedMapOf<String, SearchHit>()
    private fun remember(note: Note) {
        memories[note.id] = SearchHit("memory-${note.id}", note.id,
            listOfNotNull(note.title, note.text).distinct().joinToString("\n"), 1.0)
    }
    private val entries = linkedMapOf<String, Note>()
    private val deletedNotes = mutableMapOf<String, Long>()
    private val noteChanges = mutableMapOf<String, Long>()
    private val reminders = linkedMapOf<String, ReminderDto>()
    private val reminderChanges = mutableMapOf<String, Long>()
    private val deletedReminders = mutableMapOf<String, Long>()
    private val actions = linkedMapOf<String, SyncAction>()
    private val actionChanges = mutableMapOf<String, Long>()
    private val deletedActions = mutableMapOf<String, Long>()
    override val signedIn get() = active
    override suspend fun login(invite: String) {
        require(invite.isNotBlank()) { "请输入邀请码" }
        mockEpoch = UUID.randomUUID().toString()
        active = true
        if (seedOnLogin && reminders.isEmpty()) {
            listOf(
                Note("note-demo-1", "每周给绿植浇水", "organized", 1, title = "给绿植浇水"),
                Note("note-demo-2", "周五整理周报，回顾本周工作", "organized", 1, title = "整理周报")
            ).forEach { note ->
                entries[note.id] = note
                noteChanges[note.id] = nextSeq()
                remember(note)
            }
            seedReminder("给绿植浇水", Instant.now().plusSeconds(3600).toString())
            seedReminder("整理周报", Instant.now().plusSeconds(7200).toString())
        }
    }
    override fun beginLogout(owner: RequestOwner): LogoutSession {
        if (!isCurrent(owner)) throw StaleRequestOwner()
        active = false
        mockEpoch = UUID.randomUUID().toString()
        return LogoutSession(mockEpoch, null)
    }
    override fun withClosingSession(session: LogoutSession, block: () -> Unit) {
        if (active || session.generation != mockEpoch) throw StaleRequestOwner()
        block()
    }
    override suspend fun finishLogout(session: LogoutSession) {
        withClosingSession(session) {
        active = false; cursor = 0
        memories.clear(); organizeReads.clear()
        entries.clear(); noteChanges.clear(); deletedNotes.clear(); reminders.clear(); reminderChanges.clear()
        deletedReminders.clear(); actions.clear(); actionChanges.clear(); deletedActions.clear()
        }
    }
    private fun requireSession() { if (!active) throw SessionExpired() }
    private fun requireSession(owner: RequestOwner) {
        requireSession()
        if (!isCurrent(owner)) throw StaleRequestOwner()
    }
    private fun nextSeq() = ++cursor
    override suspend fun note(noteId: String, owner: RequestOwner): Note {
        requireSession(owner)
        require(noteId.isNotBlank()) { "记事 ID 不能为空" }
        return entries[noteId] ?: throw IllegalArgumentException("记事不存在（404）")
    }
    override suspend fun notes(owner: RequestOwner): List<Note> {
        requireSession(owner)
        organizeReads.keys.toList().forEach { id ->
            val reads = organizeReads.getValue(id) + 1
            organizeReads[id] = reads
            if (organizeAfterReads > 0 && reads >= organizeAfterReads) {
                organizeNote(id, entries.getValue(id).text.take(24), listOf("本地演示"),
                    listOf(NoteTodo("回顾这条记事")))
                seedReminder("回顾记事（本地演示）", Instant.now().plusSeconds(3600).toString(), id)
            }
        }
        return entries.values.reversed()
    }
    fun organizeNote(id: String, title: String, tags: List<String>, todos: List<NoteTodo>) {
        requireSession()
        val current = entries.getValue(id)
        entries[id] = current.copy(status = "organized", rev = current.rev + 1,
            title = title, tags = tags, todos = todos)
        remember(entries.getValue(id))
        organizeReads.remove(id)
        noteChanges[id] = nextSeq()
    }
    override suspend fun create(body: NoteCreate, owner: RequestOwner): Created {
        requireSession(owner); validText(body.text, 5000)
        val note = Note(body.client_note_id, body.text, "uploaded", 1, recorded_at = body.recorded_at, client_note_id = body.client_note_id)
        if (entries.putIfAbsent(body.client_note_id, note) == null) {
            remember(note)
            noteChanges[note.id] = nextSeq()
            organizeReads[note.id] = 0
        }
        val current = entries.getValue(body.client_note_id)
        return Created(current.id, current.status, current.rev)
    }
    override suspend fun patch(noteId: String, rev: Int, text: String?, owner: RequestOwner): Note {
        requireSession(owner)
        if (text != null) validText(text, 5000)
        val current = entries[noteId] ?: throw IllegalArgumentException("记事不存在（404）")
        require(rev >= 1 && current.rev == rev) { "版本冲突，请同步后重试" }
        if (text == null || text == current.text) return current
        val updated = current.copy(text = text, rev = current.rev + 1, status = "uploaded",
            title = null, tags = null, todos = null, category = null)
        entries[noteId] = updated
        remember(updated)
        organizeReads[noteId] = 0
        reminders.values.filter { it.note_id == noteId && it.status == "suggested" }
            .map { it.id }.forEach(::deleteReminder)
        noteChanges[noteId] = nextSeq()
        return updated
    }
    override suspend fun delete(noteId: String, owner: RequestOwner) {
        requireSession(owner)
        if (entries.remove(noteId) == null) return
        memories.remove(noteId)
        organizeReads.remove(noteId); noteChanges.remove(noteId)
        val related = reminders.values.filter { it.note_id == noteId }.map { it.id }.toSet()
        actions.values.filter { it.reminder_id in related || it.payload["note_id"] == noteId }
            .map { it.id }.forEach(::deleteAction)
        related.forEach(::deleteReminder)
        deletedNotes[noteId] = nextSeq()
    }
    override suspend fun search(query: String, topK: Int, owner: RequestOwner): List<SearchHit> {
        requireSession(owner)
        val q = query.trim(); validText(q, 500)
        require(topK in 1..50) { "搜索条数须为 1–50" }
        return memories.values.filter { it.text.contains(q, ignoreCase = true) }.take(topK)
    }
    override suspend fun ask(text: String, owner: RequestOwner): Answer {
        requireSession(owner); validText(text, 2000)
        return Answer("这是本地演示回答：你的记事已保存在本次会话中。接入服务器后可根据相关记录回答。", emptyList())
    }
    fun seedReminder(title: String, fireAt: String,
        noteId: String = "note-demo-${reminders.size + 1}", status: String = "suggested"): ReminderDto {
        requireSession(); validText(title, 256)
        val normalizedFireAt = Instant.parse(fireAt).toString()
        val now = Instant.now().toString()
        val reminder = ReminderDto("reminder-${UUID.randomUUID()}", noteId, title,
            normalizedFireAt, status = status, rev = 1, created_at = now, updated_at = now)
        reminders[reminder.id] = reminder
        reminderChanges[reminder.id] = nextSeq()
        return reminder
    }
    fun deleteReminder(id: String) {
        requireSession()
        if (reminders.remove(id) != null) {
            reminderChanges.remove(id)
            deletedReminders[id] = nextSeq()
        }
    }
    fun deleteAction(id: String) {
        requireSession()
        if (actions.remove(id) != null) {
            actionChanges.remove(id)
            deletedActions[id] = nextSeq()
        }
    }
    override suspend fun sync(since: Long, limit: Int, owner: RequestOwner): SyncResponse {
        requireSession(owner); require(since >= 0 && limit in 1..500) { "同步参数无效" }
        val changes = mutableListOf<Change>()
        noteChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "note", id) }
        deletedNotes.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "deleted_note", id) }
        reminderChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "reminder", id) }
        deletedReminders.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "deleted_reminder", id) }
        actionChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "action", id) }
        deletedActions.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "deleted_action", id) }
        changes.sortBy { it.seq }
        val page = changes.take(limit)
        val notes = mutableListOf<Note>()
        val liveReminders = mutableListOf<ReminderDto>()
        val liveActions = mutableListOf<SyncAction>()
        val removedNotes = mutableListOf<String>()
        val removedReminders = mutableListOf<String>()
        val removedActions = mutableListOf<String>()
        page.forEach { change ->
            when (change.kind) {
                "note" -> entries[change.id]?.let(notes::add)
                "reminder" -> reminders[change.id]?.let(liveReminders::add)
                "action" -> actions[change.id]?.let(liveActions::add)
                "deleted_note" -> removedNotes += change.id
                "deleted_reminder" -> removedReminders += change.id
                "deleted_action" -> removedActions += change.id
            }
        }
        return SyncResponse(page.lastOrNull()?.seq ?: since, notes, liveReminders, liveActions,
            SyncDeleted(notes = removedNotes, reminders = removedReminders, actions = removedActions), changes.size > limit)
    }
    override suspend fun cancelReminder(id: String, rev: Int, owner: RequestOwner): ReminderCancelResponse {
        requireSession(owner)
        val current = reminders[id] ?: throw IllegalArgumentException("提醒不存在")
        require(current.rev == rev) { "版本冲突，请刷新后重试" }
        val existing = actions.values.firstOrNull { it.reminder_id == id && it.type == "cancel_reminder" }
        if (current.status == "cancelled") {
            return ReminderCancelResponse(current, existing, existing?.work_item_id, duplicate = true)
        }
        require(current.status == "suggested" || current.status == "scheduled") { "提醒当前不可取消" }
        val now = Instant.now().toString()
        val updated = current.copy(status = "cancelled", rev = current.rev + 1, updated_at = now)
        reminders[id] = updated
        reminderChanges[id] = nextSeq()
        actions.values.filter { it.reminder_id == id && it.type == "schedule_reminder" }
            .map { it.id }.forEach(::deleteAction)
        val workItemId = "work-${updated.note_id}"
        val payload = mapOf<String, Any?>("reminder_id" to id, "note_id" to updated.note_id,
            "title" to updated.title, "fire_at" to updated.fire_at,
            "work_item_id" to workItemId, "rev" to updated.rev)
        val action = SyncAction("action-${UUID.randomUUID()}", "cancel_reminder", payload, workItemId, id, now)
        actions[action.id] = action
        actionChanges[action.id] = nextSeq()
        return ReminderCancelResponse(updated, action, workItemId)
    }
    override suspend fun completeReminder(id: String, rev: Int, owner: RequestOwner): ReminderCompleteResponse {
        requireSession(owner)
        val current = reminders[id] ?: throw IllegalArgumentException("提醒不存在")
        require(current.rev == rev) { "版本冲突，请刷新后重试" }
        val existing = actions.values.firstOrNull { it.reminder_id == id && it.type == "complete_reminder" }
        if (current.status == "done") {
            return ReminderCompleteResponse(current, existing, existing?.work_item_id, duplicate = true)
        }
        require(current.status == "suggested" || current.status == "scheduled") { "提醒当前不可完成" }
        val now = Instant.now().toString()
        val updated = current.copy(status = "done", rev = current.rev + 1, updated_at = now)
        reminders[id] = updated
        reminderChanges[id] = nextSeq()
        actions.values.filter { it.reminder_id == id && it.type == "schedule_reminder" }
            .map { it.id }.forEach(::deleteAction)
        val workItemId = "work-${updated.note_id}"
        val payload = mapOf<String, Any?>("reminder_id" to id, "note_id" to updated.note_id,
            "title" to updated.title, "fire_at" to updated.fire_at,
            "work_item_id" to workItemId, "rev" to updated.rev)
        val action = SyncAction("action-${UUID.randomUUID()}", "complete_reminder", payload, workItemId, id, now)
        actions[action.id] = action
        actionChanges[action.id] = nextSeq()
        return ReminderCompleteResponse(updated, action, workItemId)
    }
    override suspend fun snoozeReminder(id: String, rev: Int, minutes: Int, owner: RequestOwner): ReminderSnoozeResponse {
        requireSession(owner)
        require(minutes in 1..1440) { "贪睡分钟须为 1–1440" }
        val current = reminders[id] ?: throw IllegalArgumentException("提醒不存在")
        require(current.rev == rev) { "版本冲突，请刷新后重试" }
        require(current.status == "suggested" || current.status == "scheduled") { "提醒当前不可贪睡" }
        val now = Instant.now()
        val updated = current.copy(status = "scheduled", fire_at = now.plusSeconds(minutes * 60L).toString(),
            rev = current.rev + 1, updated_at = now.toString())
        reminders[id] = updated
        reminderChanges[id] = nextSeq()
        val workItemId = "work-${updated.note_id}"
        val payload = mapOf<String, Any?>("reminder_id" to id, "note_id" to updated.note_id,
            "title" to updated.title, "fire_at" to updated.fire_at,
            "work_item_id" to workItemId, "rev" to updated.rev)
        val existing = actions.values.firstOrNull { it.reminder_id == id && it.type == "schedule_reminder" }
        val action = existing?.copy(payload = payload, work_item_id = workItemId)
            ?: SyncAction("action-${UUID.randomUUID()}", "schedule_reminder", payload, workItemId, id, now.toString())
        actions[action.id] = action
        actionChanges[action.id] = nextSeq()
        deletedActions.remove(action.id)
        return ReminderSnoozeResponse(updated, action, workItemId)
    }
    override suspend fun confirmReminder(id: String, rev: Int, fireAt: String?, title: String?, owner: RequestOwner): ReminderConfirmResponse {
        requireSession(owner)
        val current = reminders[id] ?: throw IllegalArgumentException("提醒不存在")
        require(current.rev == rev) { "版本冲突，请刷新后重试" }
        require(current.status == "suggested" || current.status == "scheduled") { "提醒当前不可确认" }
        if (title != null) require(title.codePointCount(0, title.length) <= 256 && '\u0000' !in title) { "标题无效" }
        val effectiveTitle = title ?: current.title
        val effectiveFireAt = (fireAt ?: current.fire_at)?.let { Instant.parse(it).toString() }
        require(effectiveFireAt != null && Instant.parse(effectiveFireAt).isAfter(Instant.now())) { "提醒时间无效或已过期" }
        val changed = current.status == "suggested" || effectiveTitle != current.title || effectiveFireAt != current.fire_at
        val now = Instant.now().toString()
        val updated = current.copy(title = effectiveTitle, fire_at = effectiveFireAt, status = "scheduled",
            rev = current.rev + if (changed) 1 else 0, updated_at = if (changed) now else current.updated_at)
        if (changed) {
            reminders[id] = updated
            reminderChanges[id] = nextSeq()
        }
        val workItemId = "work-${updated.note_id}"
        val payload = mapOf<String, Any?>("reminder_id" to id, "note_id" to updated.note_id,
            "title" to updated.title, "fire_at" to updated.fire_at,
            "work_item_id" to workItemId, "rev" to updated.rev)
        val existing = actions.values.firstOrNull { it.reminder_id == id && it.type == "schedule_reminder" }
        val action = existing?.copy(payload = payload, work_item_id = workItemId)
            ?: SyncAction("action-${UUID.randomUUID()}", "schedule_reminder", payload,
                workItemId, id, now)
        if (existing == null || action != existing) {
            actions[action.id] = action
            actionChanges[action.id] = nextSeq()
            deletedActions.remove(action.id)
        }
        return ReminderConfirmResponse(updated, action, workItemId, duplicate = updated.rev == rev)
    }
}
