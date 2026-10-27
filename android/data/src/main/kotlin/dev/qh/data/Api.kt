package dev.qh.data

import retrofit2.http.*

// Wire names mirror server/app/schemas.py and api/{auth,notes,ask}.py.
data class LoginRequest(val invite_code: String, val device_id: String,
    val device_name: String = "Android", val platform: String = "android")
data class RefreshRequest(val refresh_token: String)
data class Tokens(val access_token: String, val refresh_token: String,
    val expires_in: Long, val user_id: String)
data class NoteCreate(val client_note_id: String, val text: String,
    val recorded_at: String? = null, val timezone: String? = null, val source: String = "app",
    val asr: Map<String, Any?>? = null)
data class Created(val note_id: String, val status: String, val rev: Int)
data class Note(val id: String, val text: String, val status: String,
    val rev: Int, val title: String? = null, val recorded_at: String? = null)
data class NotePage(val items: List<Note>, val next_cursor: Long?)
data class AskRequest(val text: String, val client_time: String, val timezone: String)
data class Citation(val memory_id: String, val note_id: String, val text: String)
data class Answer(val answer: String, val citations: List<Citation>)
data class ApiError(val code: String, val message: String)
data class ErrorEnvelope(val error: ApiError?)

interface AgentApi {
    @POST("v1/auth/login") suspend fun login(@Body body: LoginRequest): Tokens
    @POST("v1/auth/refresh") suspend fun refresh(@Body body: RefreshRequest): Tokens
    @POST("v1/auth/logout") suspend fun logout(@Header("Authorization") bearer: String)
    @POST("v1/notes") suspend fun create(@Header("Authorization") bearer: String, @Body body: NoteCreate): Created
    @GET("v1/notes") suspend fun notes(@Header("Authorization") bearer: String,
        @Query("cursor") cursor: Long = 0, @Query("limit") limit: Int = 100): NotePage
    @POST("v1/ask") suspend fun ask(@Header("Authorization") bearer: String, @Body body: AskRequest): Answer
}
