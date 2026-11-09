package dev.qh.agent

import android.content.Context
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import dev.qh.data.*
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import okhttp3.ResponseBody
import retrofit2.HttpException
import retrofit2.Response

internal fun httpFailure(code: Int) = HttpException(Response.error<Any>(code, ResponseBody.create(null, "{}")))

/** Distinct wire identities and authoritative revisions, never client-id-as-server-id. */
internal class ReviewServer(private val session: Repository) : Repository by session {
    val entries = linkedMapOf<String, Note>()
    val posts = mutableListOf<NoteCreate>()
    val deleted = mutableSetOf<String>()
    var patches = 0
    var failNextCreate = false
    var createGate: CompletableDeferred<Unit>? = null
    var askGate: CompletableDeferred<Unit>? = null
    override suspend fun create(body: NoteCreate, owner: RequestOwner): Created {
        posts += body
        val note = entries.values.find { it.client_note_id == body.client_note_id } ?: Note(
            "nte-server-${entries.size + 1}", body.text, "organized", 1,
            client_note_id = body.client_note_id).also { entries[it.id] = it }
        createGate?.await()
        if (failNextCreate) { failNextCreate = false; throw IOException("response loss") }
        return Created(note.id, note.status, note.rev)
    }
    override suspend fun notes(owner: RequestOwner) = entries.values.filterNot { it.id in deleted }
    override suspend fun note(noteId: String, owner: RequestOwner): Note =
        entries[noteId]?.takeUnless { it.id in deleted } ?: throw httpFailure(404)
    override suspend fun sync(since: Long, limit: Int, owner: RequestOwner) = SyncResponse(
        1, entries.values.filterNot { it.id in deleted }, deleted = SyncDeleted(notes = deleted.toList()))
    override suspend fun patch(noteId: String, rev: Int, text: String?, owner: RequestOwner): Note {
        patches++
        val current = note(noteId, owner)
        if (current.rev != rev) throw httpFailure(409)
        return current.copy(text = text ?: current.text, rev = rev + 1).also { entries[noteId] = it }
    }
    override suspend fun ask(text: String, owner: RequestOwner): Answer {
        askGate?.await()
        return Answer("真实测试回答", emptyList())
    }
}

internal class ModelFixture(val repository: Repository) : AutoCloseable {
    val context: Context = ApplicationProvider.getApplicationContext()
    val disk = RoomComposerStore(context, "review-${java.util.UUID.randomUUID()}.db")
    val model = AgentModel(repository, ReminderAlarmScheduler(context), disk)
    private val viewModels = ViewModelStore().apply { put("review", model) }
    override fun close() { viewModels.clear(); disk.close() }
}
