package dev.qh.data

import com.google.gson.Gson
import java.io.IOException
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test

class CreateRecoveryTest {
    private class Disk : ComposerStore {
        var json = ""
        var fail = false
        override fun load(owner: RequestOwner) = if (json.isEmpty()) ComposerState()
            else Gson().fromJson(json, ComposerState::class.java)
        override fun save(owner: RequestOwner, state: ComposerState) {
            if (fail) throw IOException("disk")
            json = Gson().toJson(state)
        }
        override fun clear() { json = "" }
    }
    private class Server(private val session: Repository) : Repository by session {
        val posts = mutableListOf<NoteCreate>()
        var note: Note? = null
        var loseResponse = false
        var deleted = false
        var afterCreate: suspend () -> Unit = {}
        override suspend fun create(body: NoteCreate, owner: RequestOwner): Created {
            posts += body
            if (note == null) note = Note("nte-server-distinct", body.text, "organized", 7,
                client_note_id = body.client_note_id)
            afterCreate()
            if (loseResponse) { loseResponse = false; throw IOException("response lost") }
            return Created(note!!.id, note!!.status, note!!.rev)
        }
        override suspend fun notes(owner: RequestOwner) = if (deleted) emptyList() else listOfNotNull(note)
        override suspend fun note(noteId: String, owner: RequestOwner): Note {
            if (deleted) throw retrofit2.HttpException(retrofit2.Response.error<Any>(404,
                okhttp3.ResponseBody.create(null, "")))
            return note!!
        }
    }
    @Test fun lostResponseRestartReconcileKeepsAAndIndependentB() = runBlocking {
        val session = MockRepository(false); session.login("demo")
        val repo = Server(session); val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
        val owner = repo.captureOwner()
        flow.edit(owner, ComposerDraft("A", 1))
        val a = flow.prepare(owner)
        repo.loseResponse = true
        try { flow.send(owner, a); fail() } catch (_: IOException) { }
        flow.edit(owner, ComposerDraft("B", 3))
        val restarted = CreateRecoveryFlow(repo, disk)
        assertEquals("unknown", restarted.load(owner).recovery!!.state)
        val found = restarted.retryIdentical(repo.captureOwner())!!
        assertEquals("nte-server-distinct", found.id)
        assertNotEquals(found.id, a.request.client_note_id)
        assertEquals("A", found.text)
        assertEquals("B", restarted.load(owner).draft.text)
        assertEquals(1, repo.posts.size)
        restarted.removeLocalRecovery(owner)
        assertEquals("B", restarted.load(owner).draft.text)
        assertNull(restarted.load(owner).recovery)
    }
    @Test fun explicitRetryUsesEveryOriginalFieldAndAuthoritativeBody() = runBlocking {
        val session = MockRepository(false); session.login("demo")
        val repo = Server(session); val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
        val owner = repo.captureOwner()
        flow.edit(owner, ComposerDraft("A", 1, source = "voice_asr"))
        val a = flow.prepare(owner)
        repo.loseResponse = true
        try { flow.send(owner, a); fail() } catch (_: IOException) { }
        flow.edit(owner, ComposerDraft("B", 4))
        // A is temporarily absent from live listing, but replay still returns its stable server ID.
        repo.deleted = true
        try { flow.retryIdentical(owner); fail() } catch (e: retrofit2.HttpException) { assertEquals(404, e.code()) }
        assertEquals(2, repo.posts.size)
        assertEquals(repo.posts[0], repo.posts[1])
        assertEquals("deleted", flow.load(owner).recovery!!.state)
        assertEquals("B", flow.load(owner).draft.text)
        try { flow.retryIdentical(owner); fail() } catch (_: IllegalStateException) { }
        assertEquals(2, repo.posts.size)
    }
    @Test fun diskFailureDoesNotPermitSendAndSendingRestartsAsUnknown() = runBlocking {
        val session = MockRepository(false); session.login("demo")
        val repo = Server(session); val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
        val owner = repo.captureOwner()
        flow.edit(owner, ComposerDraft("A", 1)); disk.fail = true
        try { flow.prepare(owner); fail() } catch (_: IOException) { }
        assertTrue(repo.posts.isEmpty()); assertEquals("A", disk.load(owner).draft.text)
        disk.fail = false; val a = flow.prepare(owner)
        assertEquals("sending", disk.load(owner).recovery!!.state)
        assertEquals("unknown", CreateRecoveryFlow(repo, disk).load(owner).recovery!!.state)
        assertEquals(a.request, disk.load(owner).recovery!!.request)
    }
    @Test fun editingBWhileAIsSuspendedNeverChangesRequestOrConsumesB() = runBlocking {
        val session = MockRepository(false); session.login("demo")
        val repo = Server(session); val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
        val owner = repo.captureOwner()
        flow.edit(owner, ComposerDraft("A", 1))
        val a = flow.prepare(owner)
        val started = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
        repo.afterCreate = { started.complete(Unit); finish.await() }
        val sending = async { flow.send(owner, a) }
        started.await()
        flow.edit(owner, ComposerDraft("B typed while busy", 3))
        finish.complete(Unit)
        assertEquals("A", sending.await().text)
        assertEquals("B typed while busy", flow.load(owner).draft.text)
        assertEquals(a.request, repo.posts.single())
    }

    @Test fun realRepositoryReconcilesDistinctServerIdAfterResponseLoss() = runBlocking {
        okhttp3.mockwebserver.MockWebServer().use { server ->
            server.start()
            val store = object : TokenStore {
                var tokens: Tokens? = Tokens("a", "r", 60, "u")
                override fun identity() = tokens?.let { SessionIdentity("u", "epoch") }
                override fun closeSession() { }
        override fun bumpLoginEpoch() { }
                override fun read() = tokens
                override fun write(tokens: Tokens?) { this.tokens = tokens }
                override fun deviceId() = "device"
            }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk); val owner = repo.captureOwner()
            flow.edit(owner, ComposerDraft("A", 1)); val a = flow.prepare(owner)
            server.enqueue(okhttp3.mockwebserver.MockResponse().setSocketPolicy(
                okhttp3.mockwebserver.SocketPolicy.DISCONNECT_AFTER_REQUEST))
            try { flow.send(owner, a); fail() } catch (_: IOException) { }
            val original = server.takeRequest()
            assertEquals("/v1/notes", original.path)
            assertEquals("Bearer a", original.getHeader("Authorization"))
            val posted = Gson().fromJson(original.body.readUtf8(), NoteCreate::class.java)
            assertEquals(a.request, posted)
            flow.edit(owner, ComposerDraft("B", 3))
            val authoritative = Note("nte-real-server-id", "A edited remotely", "organized", 9,
                client_note_id = a.request.client_note_id)
            server.enqueue(okhttp3.mockwebserver.MockResponse().setBody(Gson().toJson(
                NotePage(listOf(authoritative), null))).setHeader("Content-Type", "application/json"))
            server.enqueue(okhttp3.mockwebserver.MockResponse().setBody(Gson().toJson(authoritative))
                .setHeader("Content-Type", "application/json"))
            val note = flow.retryIdentical(repo.captureOwner())!!
            assertEquals(authoritative, note)
            assertNotEquals(a.request.client_note_id, note.id)
            assertEquals("B", flow.load(owner).draft.text)
            assertTrue(server.takeRequest().path!!.startsWith("/v1/notes?"))
            assertEquals("/v1/notes/nte-real-server-id", server.takeRequest().path)
            assertEquals(3, server.requestCount) // no second POST
        }
    }

}
