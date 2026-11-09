package dev.qh.agent

import dev.qh.data.*
import java.lang.reflect.Proxy
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** RealRepository + real Room + model; wire serialization is separately exercised by MockWebServer tests. */
@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class RealRecoveryModelTest {
    @Test fun realRepositoryModel409Then404ReprojectsAuthoritativeADropsStaleAffordanceAndKeepsB() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            var current: Note? = null
            var deleted = false
            var posts = 0
            var patches = 0
            val api = Proxy.newProxyInstance(AgentApi::class.java.classLoader, arrayOf(AgentApi::class.java)) { _, method, args ->
                when (method.name) {
                    "create" -> {
                        posts++
                        val body = args!![1] as NoteCreate
                        current = Note("nte-server-distinct-A", body.text, "organized", 1, client_note_id = body.client_note_id)
                        Created(current!!.id, "organized", 1)
                    }
                    "getNote" -> if (deleted) throw httpFailure(404) else current!!
                    "notes" -> NotePage(if (deleted) emptyList() else listOfNotNull(current), null)
                    "sync" -> SyncResponse(1, if (deleted) emptyList() else listOfNotNull(current),
                        deleted = SyncDeleted(notes = if (deleted) listOf(current!!.id) else emptyList()))
                    "patchNote" -> { patches++; throw httpFailure(409) }
                    else -> error("Unexpected test API ${method.name}")
                }
            } as AgentApi
            val store = object : TokenStore {
                override fun identity() = SessionIdentity("synthetic-user", "epoch")
                override fun read() = Tokens("synthetic-access", "synthetic-refresh", 60, "synthetic-user")
                override fun write(tokens: Tokens?) { }
                override fun closeSession() { }
                override fun bumpLoginEpoch() { }
                override fun deviceId() = "synthetic-device"
            }
            val repo = RealRepository(api, store, Mutex())
            ModelFixture(repo).use { f ->
                val m = f.model
                m.updateDraft("A"); m.save {}; advanceUntilIdle()
                assertNotEquals(current!!.id, current!!.client_note_id)
                m.updateDraft("B")
                current = current!!.copy(text = "authoritative remote A", rev = 7)
                m.updateRecovered(); advanceUntilIdle()
                assertEquals(1, patches)
                assertEquals(7, m.recoveredNote!!.rev)
                assertEquals("authoritative remote A", m.notes.single().text)
                assertEquals("B", m.draft)
                deleted = true
                m.reconcileCreate(); advanceUntilIdle()
                assertNull(m.recoveredNote)
                assertTrue(m.notes.isEmpty())
                assertEquals("deleted", m.recovery!!.state)
                assertEquals("B", f.disk.load(repo.captureOwner()).draft.text)
                m.updateRecovered(); advanceUntilIdle()
                assertEquals(1, patches)
                assertEquals(1, posts)
            }
        } finally { Dispatchers.resetMain() }
    }
}
