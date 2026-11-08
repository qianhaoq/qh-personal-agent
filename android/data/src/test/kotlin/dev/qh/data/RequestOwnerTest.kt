package dev.qh.data

import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import org.junit.Assert.*
import org.junit.Test
import java.lang.reflect.Proxy

class RequestOwnerTest {
    private class Store : TokenStore {
        var tokens: Tokens? = Tokens("a", "r", 60, "same-user")
        var closed = false
        var epoch = 1
        var reads = 0
        override fun identity() = tokens?.let { SessionIdentity(it.user_id, epoch.toString(), closed) }
        override fun closeSession() { epoch++; closed = true }
        override fun bumpLoginEpoch() { epoch++ }
        override fun read(): Tokens? { reads++; return tokens }
        override fun write(tokens: Tokens?) { this.tokens = tokens; if (tokens != null) closed = false }
        override fun deviceId() = "device"
    }
    private fun api(body: (String) -> Any?): AgentApi = Proxy.newProxyInstance(
        AgentApi::class.java.classLoader, arrayOf(AgentApi::class.java)
    ) { _, method, _ -> body(method.name) } as AgentApi

    @Test fun staleWaiterNeverReadsNewCredentialsOrCallsHttp() = runBlocking {
        for (operation in listOf("ask", "create", "note", "logout")) {
            val store = Store(); val mutex = Mutex(locked = true)
            var calls = 0
            val repo = RealRepository(api { calls++; error("No HTTP allowed") }, store, mutex)
            val owner = repo.captureOwner()
            val waiter = async(start = CoroutineStart.UNDISPATCHED) {
                try {
                    when (operation) {
                        "ask" -> repo.ask("question", owner)
                        "create" -> repo.create(noteDraft("A"), owner)
                        "note" -> repo.note("server-id", owner)
                        else -> repo.logout(owner)
                    }
                    false
                } catch (_: StaleRequestOwner) { true }
            }
            // Same user, different login: metadata changes while the old request waits.
            store.bumpLoginEpoch(); store.tokens = Tokens("new-access", "new-refresh", 60, "same-user")
            mutex.unlock()
            assertTrue(operation, waiter.await())
            assertEquals(operation, 0, store.reads)
            assertEquals(operation, 0, calls)
            assertEquals("new-access", store.tokens!!.access_token)
        }
    }

    @Test fun refreshResponseCannotOverwriteDifferentLogin() = runBlocking {
        val store = Store(); var calls = 0
        val unauthorized = retrofit2.HttpException(retrofit2.Response.error<Any>(401,
            okhttp3.ResponseBody.create(null, "")))
        val repo = RealRepository(api { method ->
            calls++
            when (method) {
                "ask" -> throw unauthorized
                "refresh" -> {
                    store.bumpLoginEpoch(); store.tokens = Tokens("new", "new-r", 60, "same-user")
                    Tokens("late", "late-r", 60, "same-user")
                }
                else -> error(method)
            }
        }, store, Mutex())
        val owner = repo.captureOwner()
        try { repo.ask("question", owner); fail("Expected stale owner") }
        catch (_: StaleRequestOwner) { }
        assertEquals(2, calls)
        assertEquals("new", store.tokens!!.access_token)
        assertFalse(repo.isCurrent(owner))
    }

    @Test fun runtimeAndOperationIdentityAreImmutableAndRefreshKeepsEpoch() = runBlocking {
        val store = Store(); var asks = 0
        val unauthorized = retrofit2.HttpException(retrofit2.Response.error<Any>(401,
            okhttp3.ResponseBody.create(null, "")))
        val repo = RealRepository(api { method -> when(method) {
            "ask" -> if (asks++ == 0) throw unauthorized else Answer("answer", emptyList())
            "refresh" -> Tokens("fresh", "fresh-r", 60, "same-user")
            else -> error(method)
        } }, store, Mutex(), "real:configured-server")
        val owner = repo.captureOwner()
        assertEquals("answer", repo.ask("question", owner).answer)
        assertTrue(repo.isCurrent(owner))
        assertNotEquals(owner.operationId, repo.captureOwner().operationId)
        assertEquals("real:configured-server", owner.runtimeKey)
    }
    @Test fun stale401DoesNotUseRefreshAndLateLogoutDoesNotClearNewCredentials() = runBlocking {
        val store = Store(); var calls = 0
        val unauthorized = retrofit2.HttpException(retrofit2.Response.error<Any>(401,
            okhttp3.ResponseBody.create(null, "")))
        val repo = RealRepository(api { method ->
            calls++
            store.bumpLoginEpoch(); store.tokens = Tokens("new", "new-r", 60, "same-user")
            if (method == "ask") throw unauthorized
            Unit
        }, store, Mutex())
        try { repo.ask("question", repo.captureOwner()); fail() } catch (_: StaleRequestOwner) { }
        assertEquals(1, calls)
        repo.logout(repo.captureOwner())
        assertEquals(2, calls)
        assertEquals("new", store.tokens!!.access_token)
    }

    @Test fun logoutClosingMarkerSurvivesRestartBeforeMutexAcquisition() = runBlocking {
        val store = Store(); val mutex = Mutex(locked = true)
        val repo = RealRepository(api { error("No HTTP") }, store, mutex)
        val owner = repo.captureOwner()
        val logout = launch(start = CoroutineStart.UNDISPATCHED) { repo.logout(owner) }
        assertTrue(store.closed)
        logout.cancelAndJoin()
        val restarted = RealRepository(api { error("No HTTP") }, store, Mutex())
        assertFalse(restarted.signedIn)
        try { restarted.captureOwner(); fail() } catch (_: SessionExpired) { }
        assertEquals(0, store.reads)
    }

    @Test fun invalidInputsAreRejectedBeforeOwnerCaptureEvenWhenLoggedOut() = runBlocking {
        val store = Store().apply { tokens = null }
        val repo = RealRepository(api { error("No HTTP allowed") }, store, Mutex())
        val operations: List<suspend () -> Unit> = listOf(
            { repo.note(" ") }, { repo.search(" ") }, { repo.search("query", 0) },
            { repo.ask(" ") }, { repo.create(noteDraft(" ")) }, { repo.sync(-1) })
        operations.forEach { operation ->
            try { operation(); fail("Expected input rejection") } catch (_: IllegalArgumentException) { }
        }
        assertEquals(0, store.reads)
    }

}
