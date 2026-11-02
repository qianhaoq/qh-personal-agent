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
    fun deviceId(): String
}
class SessionExpired : IOException("会话已失效，请重新登录")
interface Repository {
    val signedIn: Boolean
    suspend fun login(invite: String)
    suspend fun logout()
    suspend fun notes(): List<Note>
    suspend fun create(body: NoteCreate)
    suspend fun ask(text: String): Answer
    suspend fun sync(since: Long = 0, limit: Int = 100): SyncResponse
    suspend fun confirmReminder(id: String, rev: Int, fireAt: String? = null,
        title: String? = null): ReminderConfirmResponse
}
fun noteDraft(text: String, id: String = UUID.randomUUID().toString()) = NoteCreate(
    id, text.trim(), Instant.now().toString(), ZoneId.systemDefault().id)
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
    private val mutex: Mutex = SessionMutex.lock) : Repository {
    // Serialize session operations process-wide: a rotated refresh token must never be replayed concurrently.
    override val signedIn get() = store.read() != null
    override suspend fun login(invite: String) = mutex.withLock {
        validText(invite.trim(), 64)
        store.write(api.login(LoginRequest(invite.trim(), store.deviceId())))
    }
    private suspend fun <T> authorized(block: suspend (String) -> T): T = mutex.withLock {
        val tokens = store.read() ?: throw SessionExpired()
        try { block("Bearer ${tokens.access_token}") }
        catch (error: HttpException) {
            if (error.code() != 401) throw error
            // An ambiguous refresh failure cannot safely be retried (server replay detection).
            val fresh = try { api.refresh(RefreshRequest(tokens.refresh_token)).also(store::write) }
            catch (error: CancellationException) { store.write(null); throw error }
            catch (error: Exception) { store.write(null); throw SessionExpired() }
            try { block("Bearer ${fresh.access_token}") }
            catch (error: HttpException) {
                if (error.code() == 401) { store.write(null); throw SessionExpired() }
                throw error
            }
        }
    }
    override suspend fun logout() = mutex.withLock {
        try { store.read()?.let { api.logout("Bearer ${it.access_token}") } }
        finally { store.write(null) }
        Unit
    }
    override suspend fun notes(): List<Note> = authorized { bearer ->
        val result = mutableListOf<Note>()
        var cursor = 0L
        do {
            val page = api.notes(bearer, cursor)
            result.addAll(page.items)
            val next = page.next_cursor ?: break
            check(next > cursor) { "Invalid pagination" }
            cursor = next
        } while (true)
        result.distinctBy { it.id }.reversed()
    }
    override suspend fun create(body: NoteCreate) {
        validText(body.text, 5000)
        authorized { api.create(it, body) }
    }
    override suspend fun ask(text: String): Answer {
        validText(text, 2000)
        return authorized { api.ask(it, askBody(text)) }
    }
    override suspend fun sync(since: Long, limit: Int): SyncResponse {
        require(since >= 0 && limit in 1..500) { "同步参数无效" }
        return authorized { bearer ->
            val notes = linkedMapOf<String, Note>()
            val reminders = linkedMapOf<String, ReminderDto>()
            val actions = linkedMapOf<String, SyncAction>()
            val deletedNotes = linkedSetOf<String>()
            val deletedReminders = linkedSetOf<String>()
            val deletedActions = linkedSetOf<String>()
            var cursor = since
            while (true) {
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
    override suspend fun confirmReminder(id: String, rev: Int, fireAt: String?, title: String?) =
        authorized { api.confirmReminder(it, id, ReminderConfirmRequest(rev, fireAt, title)) }
}
class MockRepository(private val seedOnLogin: Boolean = true) : Repository {
    private data class Change(val seq: Long, val kind: String, val id: String)
    private var active = false
    private var cursor = 0L
    private val entries = linkedMapOf<String, Note>()
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
        active = true
        if (seedOnLogin && reminders.isEmpty()) {
            seedReminder("给绿植浇水", Instant.now().plusSeconds(3600).toString())
            seedReminder("整理周报", Instant.now().plusSeconds(7200).toString())
        }
    }
    override suspend fun logout() {
        active = false; cursor = 0
        entries.clear(); noteChanges.clear(); reminders.clear(); reminderChanges.clear()
        deletedReminders.clear(); actions.clear(); actionChanges.clear(); deletedActions.clear()
    }
    private fun requireSession() { if (!active) throw SessionExpired() }
    private fun nextSeq() = ++cursor
    override suspend fun notes(): List<Note> { requireSession(); return entries.values.reversed() }
    override suspend fun create(body: NoteCreate) {
        requireSession(); validText(body.text, 5000)
        val note = Note(body.client_note_id, body.text, "uploaded", 1, recorded_at = body.recorded_at)
        if (entries.putIfAbsent(body.client_note_id, note) == null) noteChanges[note.id] = nextSeq()
    }
    override suspend fun ask(text: String): Answer {
        requireSession(); validText(text, 2000)
        return Answer("这是本地演示回答：你的记事已保存在本次会话中。接入服务器后可根据相关记录回答。", emptyList())
    }
    fun seedReminder(title: String, fireAt: String,
        noteId: String = "note-demo-${reminders.size + 1}"): ReminderDto {
        requireSession(); validText(title, 256)
        val normalizedFireAt = Instant.parse(fireAt).toString()
        val now = Instant.now().toString()
        val reminder = ReminderDto("reminder-${UUID.randomUUID()}", noteId, title,
            normalizedFireAt, status = "suggested", rev = 1, created_at = now, updated_at = now)
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
    override suspend fun sync(since: Long, limit: Int): SyncResponse {
        requireSession(); require(since >= 0 && limit in 1..500) { "同步参数无效" }
        val changes = mutableListOf<Change>()
        noteChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "note", id) }
        reminderChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "reminder", id) }
        deletedReminders.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "deleted_reminder", id) }
        actionChanges.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "action", id) }
        deletedActions.forEach { (id, seq) -> if (seq > since) changes += Change(seq, "deleted_action", id) }
        changes.sortBy { it.seq }
        val page = changes.take(limit)
        val notes = mutableListOf<Note>()
        val liveReminders = mutableListOf<ReminderDto>()
        val liveActions = mutableListOf<SyncAction>()
        val removedReminders = mutableListOf<String>()
        val removedActions = mutableListOf<String>()
        page.forEach { change ->
            when (change.kind) {
                "note" -> entries[change.id]?.let(notes::add)
                "reminder" -> reminders[change.id]?.let(liveReminders::add)
                "action" -> actions[change.id]?.let(liveActions::add)
                "deleted_reminder" -> removedReminders += change.id
                "deleted_action" -> removedActions += change.id
            }
        }
        return SyncResponse(page.lastOrNull()?.seq ?: since, notes, liveReminders, liveActions,
            SyncDeleted(reminders = removedReminders, actions = removedActions), changes.size > limit)
    }
    override suspend fun confirmReminder(id: String, rev: Int, fireAt: String?, title: String?): ReminderConfirmResponse {
        requireSession()
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
        val existing = actions.values.firstOrNull { it.reminder_id == id }
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
