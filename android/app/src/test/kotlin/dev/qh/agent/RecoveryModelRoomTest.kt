package dev.qh.agent

import dev.qh.data.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class RecoveryModelRoomTest {
    private suspend fun fixture(): Pair<ReviewServer, ModelFixture> {
        val session = MockRepository(false, 0); session.login("demo")
        val server = ReviewServer(session)
        return server to ModelFixture(server)
    }
    @Test fun failedRoomWriteKeepsInMemoryTextAndModeAcrossReloadUntilRetrySucceeds() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val (server, f) = fixture()
            f.use {
                var failWrites = false
                val store = object : ComposerStore by f.disk {
                    override fun save(owner: RequestOwner, state: ComposerState) {
                        if (failWrites) throw java.io.IOException("disk unavailable")
                        f.disk.save(owner, state)
                    }
                }
                val models = androidx.lifecycle.ViewModelStore()
                val model = AgentModel(server, ReminderAlarmScheduler(f.context), store)
                models.put("dirty", model)
                try {
                    model.updateDraft("old")
                    failWrites = true
                    model.setMode("ask")
                    model.updateDraft("new unsaved text")
                    model.reload(); advanceUntilIdle()
                    assertEquals("new unsaved text", model.draft)
                    assertEquals("ask", model.inputMode)
                    assertEquals("old", f.disk.load(server.captureOwner()).draft.text)
                    failWrites = false
                    model.updateDraft("retry saved")
                    model.reload(); advanceUntilIdle()
                    assertEquals("retry saved", model.draft)
                    assertEquals("ask", f.disk.load(server.captureOwner()).draft.mode)
                    assertEquals("retry saved", f.disk.load(server.captureOwner()).draft.text)
                    assertTrue(server.posts.isEmpty())
                } finally { models.clear() }
            }
        } finally { Dispatchers.resetMain() }
    }
    @Test fun liveRecovery404AndSyncTombstoneRemoveProjectionAndUpdateAffordanceKeepB() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            for (fromSync in listOf(false, true)) {
                val (server, f) = fixture()
                f.use {
                    val m = f.model
                    m.updateDraft("A"); m.save {}; advanceUntilIdle()
                    val a = m.recoveredNote!!
                    assertNotEquals(a.id, server.posts.single().client_note_id)
                    m.updateDraft("B"); server.deleted += a.id
                    if (fromSync) m.reload() else m.reconcileCreate()
                    advanceUntilIdle()
                    assertEquals("B", m.draft)
                    assertEquals("B", f.disk.load(server.captureOwner()).draft.text)
                    assertEquals("deleted", m.recovery!!.state)
                    assertNull(m.recoveredNote)
                    assertFalse(m.notes.any { it.id == a.id })
                    m.updateRecovered(); advanceUntilIdle()
                    assertEquals(0, server.patches)
                    assertEquals(1, server.posts.size)
                }
            }
        } finally { Dispatchers.resetMain() }
    }
    @Test fun patch409RefreshesAuthoritativeAWithoutAutomaticallyOverwritingB() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val (server, f) = fixture()
            f.use {
                val m = f.model
                m.updateDraft("A"); m.save {}; advanceUntilIdle()
                val original = m.recoveredNote!!
                m.updateDraft("B")
                server.entries[original.id] = original.copy(text = "remote A", rev = 9)
                m.updateRecovered(); advanceUntilIdle()
                assertEquals(1, server.patches)
                assertEquals("B", m.draft)
                assertEquals(9, m.recoveredNote!!.rev)
                assertEquals("remote A", m.notes.single().text)
                assertEquals("remote A", server.entries[original.id]!!.text)
                m.updateRecovered(); advanceUntilIdle() // second explicit user decision
                assertEquals(2, server.patches)
                assertEquals("B", server.entries[original.id]!!.text)
            }
        } finally { Dispatchers.resetMain() }
    }
    @Test fun saveCopyUsesNewDurableIdentityAndSurvivesLostResponseDoubleClickAndRestart() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val (server, f) = fixture()
            f.use {
                val m = f.model
                m.updateDraft("A"); m.save {}; advanceUntilIdle()
                val a = m.recoveredNote!!; val operationA = m.recovery!!.operationId
                m.updateDraft("B")
                val gate = CompletableDeferred<Unit>()
                server.createGate = gate; server.failNextCreate = true
                m.saveCopy(); m.saveCopy(); runCurrent()
                val b = f.disk.load(server.captureOwner()).recovery!!
                assertEquals("B", b.request.text)
                assertNotEquals(operationA, b.operationId)
                assertNotEquals(server.posts.first().client_note_id, b.request.client_note_id)
                assertEquals("A", server.entries[a.id]!!.text)
                assertEquals(2, server.posts.size)
                m.updateDraft("C")
                gate.complete(Unit); advanceUntilIdle()
                assertEquals("unknown", m.recovery!!.state)
                val restored = CreateRecoveryFlow(server, f.disk).load(server.captureOwner())
                assertEquals(b.request, restored.recovery!!.request)
                assertEquals("C", restored.draft.text)
                m.reconcileCreate(); advanceUntilIdle()
                assertEquals(2, server.posts.size)
                assertEquals(setOf("A", "B"), m.notes.map { it.text }.toSet())
                assertEquals("C", m.draft)
            }
        } finally { Dispatchers.resetMain() }
    }
    @Test fun unifiedVoiceMetadataAndSuccessfulAskConsumeOnlyMatchingDraftVersion() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val (server, f) = fixture()
            f.use {
                val m = f.model
                val attempt = m.beginVoice(); m.acceptVoice(attempt, "语音 A"); advanceUntilIdle()
                assertEquals("voice_asr", server.posts.single().source)
                m.setMode("ask"); m.updateDraft("first question"); m.submitComposer(); advanceUntilIdle()
                assertEquals("", m.draft)
                assertEquals("first question", m.answers.first().question)
                m.updateDraft("second question")
                server.askGate = CompletableDeferred()
                m.submitComposer(); runCurrent()
                m.updateDraft("next B")
                server.askGate!!.complete(Unit); advanceUntilIdle()
                assertEquals("next B", m.draft)
                assertEquals("next B", f.disk.load(server.captureOwner()).draft.text)
            }
        } finally { Dispatchers.resetMain() }
    }
}
