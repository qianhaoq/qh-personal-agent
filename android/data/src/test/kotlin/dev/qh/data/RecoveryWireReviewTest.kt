package dev.qh.data

import com.google.gson.Gson
import java.io.IOException
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.RecordedRequest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.Assert.*
import org.junit.Test
import retrofit2.HttpException

class RecoveryWireReviewTest {
    private class Store : TokenStore {
        override fun identity() = SessionIdentity("test-user", "epoch")
        override fun read() = Tokens("synthetic-access", "synthetic-refresh", 60, "test-user")
        override fun write(tokens: Tokens?) { }
        override fun closeSession() { }
        override fun bumpLoginEpoch() { }
        override fun deviceId() = "synthetic-device"
    }
    private class Disk : ComposerStore {
        var json = Gson().toJson(ComposerState())
        override fun load(owner: RequestOwner) = Gson().fromJson(json, ComposerState::class.java)
        override fun save(owner: RequestOwner, state: ComposerState) { json = Gson().toJson(state) }
        override fun clear() { json = Gson().toJson(ComposerState()) }
    }
    private fun MockWebServer.json(value: Any, code: Int = 200) {
        enqueue(MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(Gson().toJson(value)))
    }
    private suspend fun live(server: MockWebServer, repo: Repository, flow: CreateRecoveryFlow): Note {
        val owner = repo.captureOwner()
        flow.edit(owner, ComposerDraft("A", 1))
        val a = flow.prepare(owner)
        val note = Note("nte-authoritative-A", "A", "organized", 1, client_note_id = a.request.client_note_id)
        server.json(Created(note.id, note.status, note.rev), 202); server.json(note)
        assertEquals(note, flow.send(owner, a))
        assertNotEquals(a.request.client_note_id, note.id)
        assertEquals("POST", server.takeRequest().method)
        assertEquals("/v1/notes/${note.id}", server.takeRequest().path)
        return note
    }
    @Test fun real404AndSyncTombstoneNeverResurrectAOrConsumeB() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val repo = RealRepository(realApi(server.url("/").toString()), Store())
            val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
            val a = live(server, repo, flow); val owner = repo.captureOwner()
            flow.edit(owner, ComposerDraft("B", 3))
            server.json(NotePage(emptyList(), null)); server.json(mapOf("error" to "deleted"), 404)
            try { flow.reconcile(owner); fail() } catch (e: HttpException) { assertEquals(404, e.code()) }
            assertEquals("deleted", flow.load(owner).recovery!!.state)
            assertEquals("B", flow.load(owner).draft.text)
            server.takeRequest(); server.takeRequest()
            server.json(SyncResponse(10, deleted = SyncDeleted(notes = listOf(a.id))))
            val sync = repo.sync(owner = owner)
            assertEquals(listOf(a.id), sync.deleted.notes)
            sync.deleted.notes.forEach { flow.markDeleted(owner, it) }
            try { flow.retryIdentical(owner); fail() } catch (_: IllegalStateException) { }
            assertEquals("B", flow.load(owner).draft.text)
            assertEquals(5, server.requestCount) // create, authoritative GET, live list, 404 GET, sync
        }
    }
    @Test fun real409ReloadsAuthoritativeRevisionAndRequiresSecondExplicitPatch() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val repo = RealRepository(realApi(server.url("/").toString()), Store())
            val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
            val a = live(server, repo, flow); val owner = repo.captureOwner()
            flow.edit(owner, ComposerDraft("B", 3))
            server.json(emptyMap<String, Any>(), 409)
            try { repo.patch(a.id, a.rev, "B", owner); fail() } catch (e: HttpException) { assertEquals(409, e.code()) }
            assertEquals("PATCH", server.takeRequest().method)
            val remote = a.copy(text = "remote A", rev = 9)
            server.json(NotePage(listOf(remote), null)); server.json(remote)
            assertEquals(remote, flow.reconcile(owner))
            assertEquals("B", flow.load(owner).draft.text)
            server.takeRequest(); server.takeRequest()
            assertEquals(5, server.requestCount) // no automatic second PATCH
            val accepted = remote.copy(text = "B", rev = 10)
            server.json(NotePatchResponse(accepted, 10))
            assertEquals(accepted, repo.patch(remote.id, remote.rev, "B", repo.captureOwner()))
            val body = Gson().fromJson(server.takeRequest().body.readUtf8(), NotePatch::class.java)
            assertEquals(9, body.rev)
        }
    }
    @Test fun realSaveCopyResponseLossRestartUsesNewIdentityAndKeepsBothServerObjects() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val repo = RealRepository(realApi(server.url("/").toString()), Store())
            val disk = Disk(); val flow = CreateRecoveryFlow(repo, disk)
            val a = live(server, repo, flow)
            val old = flow.load(repo.captureOwner()).recovery!!
            val owner = repo.captureOwner(); flow.edit(owner, ComposerDraft("B", 3))
            val b = flow.prepare(owner, replaceLive = true)
            assertNotEquals(old.operationId, b.operationId)
            assertNotEquals(old.request.client_note_id, b.request.client_note_id)
            try { flow.prepare(repo.captureOwner(), replaceLive = true); fail() } catch (_: IllegalStateException) { }
            val savedB = Note("nte-authoritative-B", "B", "organized", 1, client_note_id = b.request.client_note_id)
            val objects = ConcurrentHashMap<String, Note>().apply { put(old.request.client_note_id, a) }
            val posts = CopyOnWriteArrayList<NoteCreate>()
            val requests = CopyOnWriteArrayList<String>()
            // Route by wire contract, not FIFO: a transport retransmission must never steal
            // the response intended for GET /notes. Commit before losing the 202 body.
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    val path = request.path!!.substringBefore('?')
                    requests += "${request.method} $path"
                    fun json(value: Any) = MockResponse().setHeader("Content-Type", "application/json")
                        .setBody(Gson().toJson(value))
                    return when {
                        request.method == "POST" && path == "/v1/notes" -> {
                            val body = Gson().fromJson(request.body.readUtf8(), NoteCreate::class.java)
                            posts += body
                            if (body != b.request) return MockResponse().setResponseCode(409)
                            objects.putIfAbsent(body.client_note_id, savedB)
                            // Headers were received, but EOF interrupts the accepted response body.
                            // This is realistic response loss without a pending FIFO request timeout.
                            json(Created(savedB.id, savedB.status, savedB.rev)).setResponseCode(202)
                                .setHeader("Content-Length", 4096)
                                .setSocketPolicy(SocketPolicy.DISCONNECT_AT_END)
                        }
                        request.method == "GET" && path == "/v1/notes" ->
                            json(NotePage(objects.values.sortedBy { it.id }, null))
                        request.method == "GET" && path == "/v1/notes/${savedB.id}" -> json(savedB)
                        else -> MockResponse().setResponseCode(404)
                    }
                }
            }
            try { flow.send(owner, b); fail("Accepted response must be interrupted") } catch (_: IOException) { }
            assertTrue(posts.isNotEmpty())
            assertTrue(posts.all { it == b.request }) // immutable full payload on any transport replay
            assertEquals("unknown", flow.load(owner).recovery!!.state)
            assertEquals(b.request, flow.load(owner).recovery!!.request)
            assertEquals(2, objects.size) // one logical A and one logical B, regardless of transport attempts
            assertEquals(a, objects[old.request.client_note_id])
            assertEquals(savedB, objects[b.request.client_note_id])
            assertNotEquals(savedB.id, b.request.client_note_id)
            val postCountBeforeRestart = posts.size
            flow.edit(owner, ComposerDraft("next C", 5))
            val restarted = CreateRecoveryFlow(repo, Disk().apply { json = disk.json })
            val restored = restarted.load(repo.captureOwner())
            val restoredRecovery = requireNotNull(restored.recovery)
            assertEquals(b.request, restoredRecovery.request)
            assertEquals(b.operationId, restoredRecovery.operationId)
            assertEquals(savedB, restarted.retryIdentical(repo.captureOwner()))
            val recovered = restarted.load(repo.captureOwner())
            assertEquals("next C", recovered.draft.text)
            assertEquals(5L, recovered.draft.version)
            val recoveredRequest = requireNotNull(recovered.recovery)
            assertEquals("live", recoveredRequest.state)
            assertEquals(savedB.id, recoveredRequest.discoveredNoteId)
            assertEquals(postCountBeforeRestart, posts.size) // reconciliation adds no POST
            assertEquals(listOf("GET /v1/notes", "GET /v1/notes/${savedB.id}"), requests.takeLast(2))
            assertTrue(requests.all { it == "POST /v1/notes" || it == "GET /v1/notes" ||
                it == "GET /v1/notes/${savedB.id}" })
            assertEquals(mapOf(old.request.client_note_id to a, b.request.client_note_id to savedB), objects)

        }
    }
}
