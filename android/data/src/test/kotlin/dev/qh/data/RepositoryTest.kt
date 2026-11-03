package dev.qh.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import com.google.gson.JsonParser

class RepositoryTest {
    @Test fun mockPatchChecksRevAndInvalidatesDerivedFields() = runBlocking {
        val repo = MockRepository(false, 0); repo.login("demo")
        repo.create(noteDraft("原文", "n1"))
        repo.organizeNote("n1", "标题", listOf("标签"), listOf(NoteTodo("待办")))
        val reminder = repo.seedReminder("建议", "2099-01-01T00:00:00Z", "n1")
        val scheduled = repo.seedReminder("已确认", "2099-01-01T00:00:00Z", "n1")
        repo.confirmReminder(scheduled.id, scheduled.rev)
        val current = repo.notes().single()
        val cursor = repo.sync().cursor
        assertEquals(current, repo.patch("n1", current.rev, current.text))
        assertEquals(current, repo.patch("n1", current.rev, null))
        assertEquals(cursor, repo.sync().cursor)
        for (text in listOf(null, current.text, "新文")) {
            try { repo.patch("n1", current.rev - 1, text); fail("Expected conflict") }
            catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("版本冲突")) }
        }
        val updated = repo.patch("n1", current.rev, "新文")
        assertEquals(current.rev + 1, updated.rev); assertEquals("uploaded", updated.status)
        assertEquals("新文", updated.text)
        assertNull(updated.title); assertNull(updated.tags); assertNull(updated.todos); assertNull(updated.category)
        val sync = repo.sync(cursor)
        assertEquals(listOf(reminder.id), sync.deleted.reminders)
        assertEquals(updated, sync.notes.single())
        assertEquals(scheduled.id, repo.sync().reminders.single().id)
    }
    @Test fun mockDeleteTombstonesNoteRemindersAndActions() = runBlocking {
        val repo = MockRepository(false, 1); repo.login("demo")
        repo.create(noteDraft("原文", "n1"))
        val suggested = repo.seedReminder("建议", "2099-01-01T00:00:00Z", "n1")
        val scheduled = repo.seedReminder("已确认", "2099-01-01T00:00:00Z", "n1")
        val action = repo.confirmReminder(scheduled.id, scheduled.rev).action!!
        val cursor = repo.sync().cursor
        repo.delete("n1")
        val deletedCursor = repo.sync().cursor
        repo.delete("n1")
        assertEquals(deletedCursor, repo.sync().cursor)
        assertTrue(repo.notes().isEmpty())
        val sync = repo.sync(cursor)
        assertEquals(listOf("n1"), sync.deleted.notes)
        assertEquals(setOf(suggested.id, scheduled.id), sync.deleted.reminders.toSet())
        assertEquals(listOf(action.id), sync.deleted.actions)
        assertTrue(sync.cursor > cursor)
        assertTrue(repo.sync().reminders.isEmpty()); assertTrue(repo.sync().actions.isEmpty())
        var since = cursor
        val deleted = mutableListOf<String>()
        do {
            val page = repo.sync(since, 1); since = page.cursor; deleted += page.deleted.notes
        } while (page.has_more)
        assertEquals(listOf("n1"), deleted)
        repo.logout(); repo.login("demo")
        assertTrue(repo.sync().deleted.notes.isEmpty())
        repo.logout()
        try { repo.patch("n1", 1, null); fail("Expected session error") } catch (_: SessionExpired) { }
        try { repo.delete("n1"); fail("Expected session error") } catch (_: SessionExpired) { }
    }
    @Test fun retrofitPatchDeleteAndConflictContract() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"note":{"id":"n1","text":"新文","status":"uploaded","rev":3},"rev":3}""")
            assertEquals(3, repo.patch("n1", 2, "新文").rev)
            val patch = server.takeRequest()
            assertEquals("PATCH", patch.method); assertEquals("/v1/notes/n1", patch.path)
            assertEquals("Bearer access", patch.getHeader("Authorization"))
            val body = JsonParser.parseString(patch.body.readUtf8()).asJsonObject
            assertEquals(2, body["rev"].asInt); assertEquals("新文", body["text"].asString)
            server.enqueue(MockResponse().setResponseCode(204))
            repo.delete("n1")
            val delete = server.takeRequest()
            assertEquals("DELETE", delete.method); assertEquals("/v1/notes/n1", delete.path)
            assertEquals("Bearer access", delete.getHeader("Authorization"))
            server.json("""{"error":{"code":"conflict","message":"conflict"}}""", 409)
            try { repo.patch("n1", 2, "新文"); fail("Expected conflict") }
            catch (e: retrofit2.HttpException) { assertEquals("版本冲突，请同步后重试", readableError(e)) }
        }
    }

    @Test fun retrofitDeleteTreatsOnly404AsSuccess() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.enqueue(MockResponse().setResponseCode(404))
            repo.delete("n1")
            assertTrue(repo.signedIn)
            server.enqueue(MockResponse().setResponseCode(500))
            try { repo.delete("n1"); fail("Expected server error") }
            catch (e: retrofit2.HttpException) { assertEquals(500, e.code()) }
            assertEquals(2, server.requestCount)
        }
    }

    @Test fun draftSourceDefaultsToAppAndSupportsVoice() {
        assertEquals("app", noteDraft("文字").source)
        val voice = noteDraft("  语音内容  ", "voice-id", source = "voice_asr")
        assertEquals("voice_asr", voice.source)
        assertEquals("语音内容", voice.text)
        assertEquals("voice-id", voice.client_note_id)
        assertTrue(!voice.recorded_at.isNullOrBlank())
        assertTrue(!voice.timezone.isNullOrBlank())
    }

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
    @Test fun mockSyncConfirmPaginationAndDeletedChanges() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        assertTrue(repo.sync().let { it.cursor == 0L && it.reminders.isEmpty() && it.actions.isEmpty() })
        val seeded = repo.seedReminder("吃药", "2099-01-01T00:00:00Z")
        val initial = repo.sync()
        assertEquals("suggested", initial.reminders.single().status)
        assertTrue(repo.sync(initial.cursor).let {
            it.cursor == initial.cursor && it.reminders.isEmpty() && it.actions.isEmpty() &&
                it.deleted.actions.isEmpty() && it.deleted.reminders.isEmpty()
        })
        val confirmed = repo.confirmReminder(seeded.id, seeded.rev)
        assertEquals("scheduled", confirmed.reminder.status)
        assertEquals(2, confirmed.reminder.rev)
        assertFalse(confirmed.duplicate)
        assertEquals("schedule_reminder", confirmed.action?.type)
        assertEquals(seeded.id, confirmed.action?.reminder_id)
        val delta = repo.sync(initial.cursor)
        assertEquals("scheduled", delta.reminders.single().status)
        assertEquals(confirmed.action?.id, delta.actions.single().id)
        assertTrue(delta.deleted.actions.isEmpty())
        val duplicate = repo.confirmReminder(seeded.id, confirmed.reminder.rev)
        assertTrue(duplicate.duplicate)
        assertEquals(confirmed.action?.id, duplicate.action?.id)
        assertTrue(repo.sync(delta.cursor).actions.isEmpty())
        repo.deleteAction(confirmed.action!!.id)
        val deletedAction = repo.sync(delta.cursor)
        assertEquals(listOf(confirmed.action!!.id), deletedAction.deleted.actions)
        assertTrue(deletedAction.actions.isEmpty())
        repo.deleteReminder(seeded.id)
        val deletedReminder = repo.sync(deletedAction.cursor)
        assertEquals(listOf(seeded.id), deletedReminder.deleted.reminders)
        assertTrue(deletedReminder.reminders.isEmpty())
        repo.logout()
        try { repo.sync(); fail("Expected session error") } catch (_: SessionExpired) { }
    }
    @Test fun mockOrganizesAfterReadsAndSyncsSuggestedReminder() = runBlocking {
        val repo = MockRepository(seedOnLogin = false, organizeAfterReads = 2)
        repo.login("demo")
        assertEquals(Created("client", "uploaded", 1), repo.create(noteDraft("买牛奶", "client")))
        assertEquals("uploaded", repo.notes().single().status)
        val note = repo.notes().single()
        assertEquals("organized", note.status)
        assertEquals(listOf("本地演示"), note.tags)
        assertEquals("回顾这条记事", note.todos!!.single().text)
        assertEquals(note, repo.sync().notes.single())
        assertEquals("client", repo.sync().reminders.single().note_id)
        assertEquals("suggested", repo.sync().reminders.single().status)
    }
    @Test fun gsonParsesOrganizedFieldsInListAndSync() {
        val json = """{"id":"n","text":"正文","status":"organized","rev":2,"tags":["生活"],"todos":[{"text":"买奶","due":"2099-01-01"},{"text":"散步"}]}"""
        val gson = com.google.gson.Gson()
        val note = gson.fromJson(json, Note::class.java)
        assertEquals(listOf("生活"), note.tags)
        assertEquals("2099-01-01", note.todos!![0].due)
        assertNull(note.todos!![1].due)
        assertEquals(note, gson.fromJson("""{"cursor":1,"notes":[$json]}""", SyncResponse::class.java).notes.single())
        assertEquals(note, gson.fromJson("""{"items":[$json]}""", NotePage::class.java).items.single())
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
            assertEquals(Created("n1", "uploaded", 1), repo.create(noteDraft("你好", "client-1")))
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
    @Test fun retrofitSyncPaginationAndReminderConfirmContract() = runBlocking {
        MockWebServer().use { server ->
            server.start(); val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"cursor":4,"notes":[],"reminders":[{"id":"r1","note_id":"n1","title":"吃药","fire_at":"2099-01-01T00:00:00Z","rrule":null,"status":"suggested","rev":1,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}],"actions":[],"deleted":{"notes":[],"reminders":[],"actions":[]},"has_more":true}""")
            server.json("""{"cursor":8,"notes":[],"reminders":[],"actions":[{"id":"a1","type":"schedule_reminder","payload":{"reminder_id":"r1","note_id":"n1","title":"吃药","fire_at":"2099-01-01T00:00:00Z","work_item_id":"w1","rev":2},"work_item_id":"w1","reminder_id":"r1","created_at":"2026-01-01T00:00:00Z","unknown":"ignored"}],"deleted":{"notes":["n0"],"reminders":[],"actions":[]},"has_more":false}""")
            val synced = repo.sync(since = 0, limit = 2)
            assertEquals(8, synced.cursor)
            assertEquals("r1", synced.reminders.single().id)
            assertEquals("a1", synced.actions.single().id)
            assertEquals("w1", synced.actions.single().payload["work_item_id"])
            assertEquals(listOf("n0"), synced.deleted.notes)
            assertFalse(synced.has_more)
            val first = server.takeRequest()
            assertEquals("GET", first.method); assertEquals("/v1/sync?since=0&limit=2", first.path)
            assertEquals("Bearer access", first.getHeader("Authorization"))
            assertEquals("/v1/sync?since=4&limit=2", server.takeRequest().path)
            server.json("""{"reminder":{"id":"r1","note_id":"n1","title":"吃药（晚上）","fire_at":"2099-01-01T01:00:00Z","rrule":null,"status":"scheduled","rev":3,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"},"action":{"id":"a1","type":"schedule_reminder","payload":{"reminder_id":"r1","note_id":"n1","title":"吃药（晚上）","fire_at":"2099-01-01T01:00:00Z","work_item_id":"w1","rev":3},"work_item_id":"w1","reminder_id":"r1","created_at":"2026-01-01T00:00:00Z"},"work_item_id":"w1","duplicate":false}""")
            val confirmed = repo.confirmReminder("r1", 3, "2099-01-01T01:00:00Z", "吃药（晚上）")
            assertEquals("scheduled", confirmed.reminder.status)
            assertEquals("a1", confirmed.action?.id)
            val confirm = server.takeRequest()
            assertEquals("POST", confirm.method); assertEquals("/v1/reminders/r1/confirm", confirm.path)
            assertEquals("Bearer access", confirm.getHeader("Authorization"))
            val body = JsonParser.parseString(confirm.body.readUtf8()).asJsonObject
            assertEquals(3, body["rev"].asInt)
            assertEquals("2099-01-01T01:00:00Z", body["fire_at"].asString)
            assertEquals("吃药（晚上）", body["title"].asString)
        }
    }
    @Test fun syncKeepsLatestReminderAcrossPages() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json(syncPage(1, "[" + reminderJson("r1", 1, "Old") + "]", more = true))
            server.json(syncPage(2, "[" + reminderJson("r1", 2, "Latest") + "]"))
            val result = repo.sync()
            assertEquals(2L, result.cursor)
            assertFalse(result.has_more)
            assertEquals("r1", result.reminders.single().id)
            assertEquals(2, result.reminders.single().rev)
            assertEquals("Latest", result.reminders.single().title)
        }
    }
    @Test fun syncRemovesReminderWhenLaterPageContainsTombstone() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json(syncPage(1, "[" + reminderJson("r1", 1) + "]", more = true))
            server.json(syncPage(2, "[]", deleted = """["r1"]"""))
            val result = repo.sync()
            assertTrue(result.reminders.isEmpty())
            assertEquals(listOf("r1"), result.deleted.reminders)
        }
    }
    private fun reminderJson(id: String, rev: Int, title: String = "提醒") =
        """{"id":"$id","note_id":"n1","title":"$title","fire_at":"2099-01-01T00:00:00Z","rrule":null,"status":"suggested","rev":$rev,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"""
    private fun syncPage(cursor: Long, reminders: String, deleted: String = "[]", more: Boolean = false) =
        """{"cursor":$cursor,"notes":[],"reminders":$reminders,"actions":[],"deleted":{"notes":[],"reminders":$deleted,"actions":[]},"has_more":$more}"""

    @Test fun apiErrorsHaveReadableMessage() = runBlocking {
        MockWebServer().use { server ->
            server.start(); server.json("""{"error":{"code":"forbidden","message":"邀请码已过期"}}""", 403)
            try { realApi(server.url("/").toString()).login(LoginRequest("demo", "device")); fail("Expected HTTP error") }
            catch (e: retrofit2.HttpException) { assertEquals("邀请码已过期", readableError(e)) }
        }
    }
}
