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

class RealRepository(private val api: AgentApi, private val store: TokenStore) : Repository {
    // Serialize session operations: a rotated refresh token must never be replayed concurrently.
    private val mutex = Mutex()
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
            catch (error: kotlinx.coroutines.CancellationException) { store.write(null); throw error }
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
}
class MockRepository : Repository {
    private var active = false
    private val entries = linkedMapOf<String, Note>()
    override val signedIn get() = active
    override suspend fun login(invite: String) { require(invite.isNotBlank()) { "请输入邀请码" }; active = true }
    override suspend fun logout() { active = false; entries.clear() }
    private fun requireSession() { if (!active) throw SessionExpired() }
    override suspend fun notes(): List<Note> { requireSession(); return entries.values.reversed() }
    override suspend fun create(body: NoteCreate) {
        requireSession(); validText(body.text, 5000)
        entries.putIfAbsent(body.client_note_id, Note(body.client_note_id, body.text, "uploaded", 1,
            recorded_at = body.recorded_at))
    }
    override suspend fun ask(text: String): Answer {
        requireSession(); validText(text, 2000)
        return Answer("这是本地演示回答：你的记事已保存在本次会话中。接入服务器后可根据相关记录回答。", emptyList())
    }
}
