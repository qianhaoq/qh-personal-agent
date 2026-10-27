package dev.qh.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import com.google.gson.JsonParser

class RepositoryTest {
    @Test fun mockRequiresLoginAndKeepsIdempotentNotesUntilLogout() = runBlocking {
        val repo = MockRepository()
        try { repo.notes(); fail("Expected session error") } catch (_: SessionExpired) { }
        try { repo.login("  "); fail("Expected validation") } catch (_: IllegalArgumentException) { }
        repo.login("demo")
        val first = noteDraft("买牛奶", "client-1")
        repo.create(first); repo.create(first.copy(text = "重复提交"))
        repo.create(noteDraft("写日记", "client-2"))
        assertEquals(listOf("写日记", "买牛奶"), repo.notes().map { it.text })
        assertTrue(repo.ask("今天做什么？").answer.contains("本地演示"))
        repo.logout(); assertFalse(repo.signedIn)
        repo.login("another-demo"); assertTrue(repo.notes().isEmpty())
    }
    @Test fun mockRejectsEmptyAndOversizeInput() = runBlocking {
        val repo = MockRepository(); repo.login("demo")
        for (text in listOf(" ", "x".repeat(5001), "bad\u0000text")) {
            try { repo.create(noteDraft(text)); fail("Expected validation") } catch (_: IllegalArgumentException) { }
        }
        try { repo.ask("x".repeat(2001)); fail("Expected validation") } catch (_: IllegalArgumentException) { }
        assertTrue(repo.notes().isEmpty())
    }
    private class Store : TokenStore {
        var tokens: Tokens? = null
        override fun read() = tokens
        override fun write(tokens: Tokens?) { this.tokens = tokens }
        override fun deviceId() = "stable-device"
    }
    private fun MockWebServer.json(body: String, code: Int = 200) {
        enqueue(MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body))
    }
    private val tokens = """{"access_token":"access-1","refresh_token":"refresh-1","expires_in":900,"user_id":"u1"}"""
    @Test fun retrofitContractMatchesLoginCreatePaginationAskAndLogout() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store(); val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json(tokens); repo.login("demo")
            val login = server.takeRequest()
            assertEquals("POST", login.method); assertEquals("/v1/auth/login", login.path)
            val body = JsonParser.parseString(login.body.readUtf8()).asJsonObject
            assertEquals("demo", body["invite_code"].asString)
            assertEquals("stable-device", body["device_id"].asString)
            assertEquals("android", body["platform"].asString)
            server.json("""{"note_id":"n1","status":"uploaded","rev":1}""", 202)
            repo.create(noteDraft("你好", "client-1"))
            val create = server.takeRequest()
            assertEquals("/v1/notes", create.path); assertEquals("Bearer access-1", create.getHeader("Authorization"))
            val note = JsonParser.parseString(create.body.readUtf8()).asJsonObject
            assertEquals("client-1", note["client_note_id"].asString)
            assertEquals("你好", note["text"].asString)
            assertEquals("app", note["source"].asString)
            assertTrue(note.has("recorded_at") && note.has("timezone"))
            server.json("""{"items":[{"id":"n1","text":"你好","title":null,"status":"uploaded","rev":1}],"next_cursor":7}""")
            server.json("""{"items":[{"id":"n2","text":"第二条","status":"ready","rev":2}],"next_cursor":null}""")
            assertEquals(listOf("n2", "n1"), repo.notes().map { it.id })
            assertEquals("/v1/notes?cursor=0&limit=100", server.takeRequest().path)
            assertEquals("/v1/notes?cursor=7&limit=100", server.takeRequest().path)
            server.json("""{"answer":"你好","citations":[{"memory_id":"m1","note_id":"n1","text":"你好"}]}""")
            assertEquals("n1", repo.ask("记了什么").citations.single().note_id)
            val ask = server.takeRequest(); assertEquals("/v1/ask", ask.path)
            val question = JsonParser.parseString(ask.body.readUtf8()).asJsonObject
            assertEquals("记了什么", question["text"].asString)
            assertTrue(question.has("client_time") && question.has("timezone"))
            server.enqueue(MockResponse().setResponseCode(204)); repo.logout()
            assertEquals("/v1/auth/logout", server.takeRequest().path); assertFalse(repo.signedIn)
        }
    }
    @Test fun refreshRotatesOnceAndRetriesWithNewBearer() = runBlocking {
        MockWebServer().use { server ->
            server.start(); val store = Store().apply { tokens = Tokens("old", "rotation-1", 1, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"error":{"code":"unauthorized","message":"expired"}}""", 401)
            server.json(tokens)
            server.json("""{"items":[],"next_cursor":null}""")
            assertTrue(repo.notes().isEmpty())
            assertEquals("Bearer old", server.takeRequest().getHeader("Authorization"))
            val refresh = server.takeRequest()
            assertEquals("/v1/auth/refresh", refresh.path)
            assertEquals("rotation-1", JsonParser.parseString(refresh.body.readUtf8()).asJsonObject["refresh_token"].asString)
            assertEquals("Bearer access-1", server.takeRequest().getHeader("Authorization"))
            assertEquals("refresh-1", store.tokens?.refresh_token)
        }
    }
    @Test fun rejectedRefreshClearsSessionAndDoesNotLoop() = runBlocking {
        MockWebServer().use { server ->
            server.start(); val store = Store().apply { tokens = Tokens("old", "revoked", 1, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            repeat(2) { server.json("""{"error":{"code":"unauthorized","message":"expired"}}""", 401) }
            try { repo.notes(); fail("Expected session error") } catch (e: SessionExpired) {
                assertTrue(readableError(e).contains("重新登录"))
            }
            assertFalse(repo.signedIn); assertEquals(2, server.requestCount)
        }
    }
    @Test fun apiErrorsHaveReadableMessage() = runBlocking {
        MockWebServer().use { server ->
            server.start(); server.json("""{"error":{"code":"forbidden","message":"邀请码已过期"}}""", 403)
            try { realApi(server.url("/").toString()).login(LoginRequest("demo", "device")); fail("Expected HTTP error") }
            catch (e: retrofit2.HttpException) { assertEquals("邀请码已过期", readableError(e)) }
        }
    }
}
