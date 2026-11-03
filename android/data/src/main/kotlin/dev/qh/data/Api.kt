package dev.qh.data

import retrofit2.http.*

// Wire names mirror server/app/schemas.py and api/{auth,notes,ask,sync,reminders}.py.
data class LoginRequest(val invite_code: String, val device_id: String,
    val device_name: String = "Android", val platform: String = "android")
data class RefreshRequest(val refresh_token: String)
data class Tokens(val access_token: String, val refresh_token: String,
    val expires_in: Long, val user_id: String)
data class NoteCreate(val client_note_id: String, val text: String,
    val recorded_at: String? = null, val timezone: String? = null, val source: String = "app",
    val asr: Map<String, Any?>? = null)
data class Created(val note_id: String, val status: String, val rev: Int)
data class NoteTodo(val text: String, val due: String? = null)
data class Note(val id: String, val text: String, val status: String,
    val rev: Int, val title: String? = null, val recorded_at: String? = null,
    val tags: List<String>? = null, val todos: List<NoteTodo>? = null, val category: String? = null)
data class NotePatch(val rev: Int, val text: String? = null)
data class NotePatchResponse(val note: Note, val rev: Int)
data class NotePage(val items: List<Note>, val next_cursor: Long?)
data class AskRequest(val text: String, val client_time: String, val timezone: String)
data class Citation(val memory_id: String, val note_id: String, val text: String)
data class SearchHit(val memory_id: String, val note_id: String?, val text: String, val score: Double?)
data class SearchResponse(val items: List<SearchHit>)
data class Answer(val answer: String, val citations: List<Citation>)
data class ReminderDto(val id: String, val note_id: String?, val title: String,
    val fire_at: String?, val rrule: String? = null, val status: String, val rev: Int,
    val created_at: String? = null, val updated_at: String? = null)
data class SyncAction(val id: String, val type: String,
    val payload: Map<String, Any?> = emptyMap(), val work_item_id: String? = null,
    val reminder_id: String? = null, val created_at: String? = null)
data class SyncDeleted(val notes: List<String> = emptyList(),
    val reminders: List<String> = emptyList(), val actions: List<String> = emptyList())
data class SyncResponse(val cursor: Long, val notes: List<Note> = emptyList(),
    val reminders: List<ReminderDto> = emptyList(), val actions: List<SyncAction> = emptyList(),
    val deleted: SyncDeleted = SyncDeleted(), val has_more: Boolean = false)
data class ReminderConfirmRequest(val rev: Int, val fire_at: String? = null, val title: String? = null)
data class ReminderConfirmResponse(val reminder: ReminderDto, val action: SyncAction? = null,
    val work_item_id: String? = null, val duplicate: Boolean = false)
data class ApiError(val code: String, val message: String)
data class ErrorEnvelope(val error: ApiError?)

interface AgentApi {
    @POST("v1/auth/login") suspend fun login(@Body body: LoginRequest): Tokens
    @POST("v1/auth/refresh") suspend fun refresh(@Body body: RefreshRequest): Tokens
    @POST("v1/auth/logout") suspend fun logout(@Header("Authorization") bearer: String)
    @POST("v1/notes") suspend fun create(@Header("Authorization") bearer: String, @Body body: NoteCreate): Created
    @GET("v1/notes/{id}") suspend fun getNote(@Header("Authorization") bearer: String, @Path("id") id: String): Note
    @PATCH("v1/notes/{id}") suspend fun patchNote(@Header("Authorization") bearer: String,
        @Path("id") id: String, @Body body: NotePatch): NotePatchResponse
    @DELETE("v1/notes/{id}") suspend fun deleteNote(@Header("Authorization") bearer: String,
        @Path("id") id: String)
    @GET("v1/notes") suspend fun notes(@Header("Authorization") bearer: String,
        @Query("cursor") cursor: Long = 0, @Query("limit") limit: Int = 100): NotePage
    @POST("v1/ask") suspend fun ask(@Header("Authorization") bearer: String, @Body body: AskRequest): Answer
    @GET("v1/search") suspend fun search(@Header("Authorization") bearer: String,
        @Query("q") q: String, @Query("top_k") topK: Int = 10): SearchResponse
    @GET("v1/sync") suspend fun sync(@Header("Authorization") bearer: String,
        @Query("since") since: Long = 0, @Query("limit") limit: Int = 100): SyncResponse
    @POST("v1/reminders/{id}/confirm") suspend fun confirmReminder(@Header("Authorization") bearer: String,
        @Path("id") id: String, @Body body: ReminderConfirmRequest): ReminderConfirmResponse
}
