package dev.qh.agent

import android.content.Context
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import dev.qh.data.*
import java.io.IOException
import java.lang.reflect.Proxy
import java.util.UUID
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class LogoutRoomModelTest {
    /** Synthetic test credentials in isolated Robolectric prefs, never production SecureTokens. */
    private class DurableTokens(context: Context, name: String) : TokenStore {
        private val prefs = context.getSharedPreferences(name, Context.MODE_PRIVATE)
        var reads = 0
        override fun identity() = prefs.getString("user", null)?.let {
            SessionIdentity(it, prefs.getString("epoch", "initial")!!, prefs.getBoolean("closed", false))
        }
        override fun closeSession() {
            check(prefs.edit().putString("epoch", UUID.randomUUID().toString()).putBoolean("closed", true).commit())
        }
        override fun bumpLoginEpoch() { check(prefs.edit().putString("epoch", UUID.randomUUID().toString()).commit()) }
        override fun read(): Tokens? {
            reads++
            return prefs.getString("access", null)?.let { Tokens(it, "synthetic-refresh", 60, "same-user") }
        }
        override fun write(tokens: Tokens?) {
            val edit = prefs.edit()
            if (tokens == null) edit.remove("access").remove("user") else
                edit.putString("access", tokens.access_token).putString("user", tokens.user_id).putBoolean("closed", false)
            check(edit.commit())
        }
        override fun deviceId() = "synthetic-device"
    }
    private fun api(onLogout: () -> Unit = {}): AgentApi = Proxy.newProxyInstance(
        AgentApi::class.java.classLoader, arrayOf(AgentApi::class.java)) { _, method, _ ->
        when (method.name) {
            "logout" -> { onLogout(); Unit }
            "login" -> Tokens("synthetic-new", "synthetic-refresh", 60, "same-user")
            else -> error("Unexpected test HTTP: ${method.name}")
        }
    } as AgentApi

    @Test fun modelClosesDurablyBeforeItsQueuedRoomOrAlarmCleanupAndRestartRejectsOldCredentials() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val context: Context = ApplicationProvider.getApplicationContext()
            val name = "logout-${UUID.randomUUID()}"
            val tokens = DurableTokens(context, name)
            tokens.write(Tokens("synthetic-old", "synthetic-r", 60, "same-user"))
            val repo = RealRepository(api(), tokens, Mutex(locked = true))
            val oldOwner = repo.captureOwner()
            ModelFixture(repo).use { f ->
                f.disk.save(oldOwner, ComposerState(ComposerDraft("A", 1)))
                f.model.logout() // Standard dispatcher has not executed cleanup at all.
                assertTrue(tokens.identity()!!.closed)
                assertNotEquals(oldOwner.loginEpoch, tokens.identity()!!.loginEpoch)
                assertFalse(repo.isCurrent(oldOwner))
                assertEquals(0, tokens.reads)
                assertEquals("A", f.disk.load(oldOwner).draft.text)
                val restarted = RealRepository(api(), DurableTokens(context, name), Mutex())
                assertFalse(restarted.signedIn)
                try { restarted.captureOwner(); fail() } catch (_: SessionExpired) { }
            } // cancel the not-yet-run model scope, representing loss before cleanup
        } finally { Dispatchers.resetMain() }
    }

    @Test fun roomCleanupFailureCannotReopenSessionAndLateCleanupCannotEraseNewLoginDraft() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val context: Context = ApplicationProvider.getApplicationContext()
            val tokens = DurableTokens(context, "logout-${UUID.randomUUID()}")
            tokens.write(Tokens("synthetic-old", "r", 60, "same-user"))
            var logoutCalls = 0
            val repo = RealRepository(api { logoutCalls++ }, tokens, Mutex())
            val owner = repo.captureOwner()
            val dbName = "room-${UUID.randomUUID()}.db"
            RoomComposerStore(context, dbName).use { disk ->
                disk.save(owner, ComposerState(ComposerDraft("A", 1)))
                val failing = object : ComposerStore by disk {
                    override fun clear() { assertTrue(tokens.identity()!!.closed); throw IOException("injected cleanup") }
                }
                val model = AgentModel(repo, ReminderAlarmScheduler(context), failing)
                val models = ViewModelStore().apply { put("model", model) }
                try {
                    model.logout(); advanceUntilIdle()
                    assertFalse(repo.signedIn)
                    assertEquals(1, logoutCalls)
                } finally { models.clear() }
            }
            RoomComposerStore(context, dbName).use { assertEquals("A", it.load(owner).draft.text) }
            repo.login("demo")
            ModelFixture(repo).use { f ->
                f.model.logout() // closes old login immediately, cleanup remains queued
                repo.login("demo") // same-user new login before old cleanup
                val newOwner = repo.captureOwner()
                CreateRecoveryFlow(repo, f.disk).edit(newOwner, ComposerDraft("new B", 1))
                val before = logoutCalls
                advanceUntilIdle()
                assertTrue(repo.isCurrent(newOwner))
                assertEquals("new B", f.disk.load(newOwner).draft.text)
                assertEquals(before, logoutCalls)
            }
        } finally { Dispatchers.resetMain() }
    }

    @Test fun actualRoomSnapshotTransactionReopensCompleteAAndPreservesBAndEpochIsolation() = runTest {
        val context: Context = ApplicationProvider.getApplicationContext()
        val session = MockRepository(false, 0); session.login("demo")
        val owner = session.captureOwner()
        val name = "room-${UUID.randomUUID()}.db"
        lateinit var a: CreateRecovery
        RoomComposerStore(context, name).use { disk ->
            val flow = CreateRecoveryFlow(session, disk)
            flow.edit(owner, ComposerDraft("A", 1, source = "voice_asr"))
            a = flow.prepare(owner)
            flow.edit(owner, ComposerDraft("B", 3))
        }
        RoomComposerStore(context, name).use { disk ->
            val state = CreateRecoveryFlow(session, disk).load(owner)
            val restoredRecovery = requireNotNull(state.recovery)
            assertEquals(a.request, restoredRecovery.request)
            assertEquals("unknown", restoredRecovery.state)
            assertEquals("B", state.draft.text)
            session.logout(); session.login("demo")
            assertEquals(ComposerState(), disk.load(session.captureOwner()))
        }
    }
    @Test fun realRoomWriteAndLogoutShareOneCommitOrderAndStaleWriteNeverRepopulates() {
        val context: Context = ApplicationProvider.getApplicationContext()
        val tokens = DurableTokens(context, "logout-${UUID.randomUUID()}")
        tokens.write(Tokens("synthetic-old", "r", 60, "same-user"))
        val repo = RealRepository(api(), tokens, Mutex())
        val owner = repo.captureOwner()
        val entered = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val pool = java.util.concurrent.Executors.newFixedThreadPool(2)
        RoomComposerStore(context, "race-${UUID.randomUUID()}.db").use { disk ->
            val held = object : ComposerStore by disk {
                override fun save(owner: RequestOwner, state: ComposerState) {
                    entered.countDown()
                    check(release.await(5, java.util.concurrent.TimeUnit.SECONDS))
                    disk.save(owner, state)
                }
            }
            try {
                val writer = pool.submit { CreateRecoveryFlow(repo, held).edit(owner, ComposerDraft("B", 2)) }
                assertTrue(entered.await(5, java.util.concurrent.TimeUnit.SECONDS))
                val logout = pool.submit {
                    val receipt = repo.beginLogout(owner)
                    repo.withClosingSession(receipt) { disk.clear() }
                }
                release.countDown()
                writer.get(5, java.util.concurrent.TimeUnit.SECONDS)
                logout.get(5, java.util.concurrent.TimeUnit.SECONDS)
                assertEquals(ComposerState(), disk.load(owner))
                try { CreateRecoveryFlow(repo, disk).edit(owner, ComposerDraft("late", 3)); fail() }
                catch (_: StaleRequestOwner) { }
                assertEquals(ComposerState(), disk.load(owner))
                assertEquals(0, tokens.reads)
            } finally { release.countDown(); pool.shutdownNow() }
        }
    }

}
