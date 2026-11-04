package dev.qh.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import com.google.gson.JsonParser

class RepositoryTest {
    @Test fun mockCancelScheduledTombstonesActionAndIsIdempotent() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val seeded = repo.seedReminder("吃药", "2099-01-01T00:00:00Z")
        val confirmed = repo.confirmReminder(seeded.id, seeded.rev)
        val before = repo.sync()
        val cancelled = repo.cancelReminder(seeded.id, confirmed.reminder.rev)
        assertEquals("cancelled", cancelled.reminder.status)
        assertEquals(3, cancelled.reminder.rev)
        assertFalse(cancelled.duplicate)
        assertEquals("cancel_reminder", cancelled.action?.type)
        assertEquals(3, cancelled.action?.payload?.get("rev"))
        val delta = repo.sync(before.cursor)
        assertEquals(listOf(confirmed.action!!.id), delta.deleted.actions)
        assertEquals(cancelled.action, delta.actions.single())
        assertEquals(cancelled.reminder, delta.reminders.single())
        val retry = repo.cancelReminder(seeded.id, cancelled.reminder.rev)
        assertTrue(retry.duplicate)
        assertEquals(cancelled.action, retry.action)
        assertEquals(cancelled.reminder, retry.reminder)
        assertEquals(delta.cursor, repo.sync(delta.cursor).cursor)
    }
    @Test fun mockCancelSuggestedAndRejectStaleRev() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val seeded = repo.seedReminder("吃药", "2099-01-01T00:00:00Z")
        try { repo.cancelReminder(seeded.id, 2); fail("Expected conflict") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("版本冲突")) }
        val cancelled = repo.cancelReminder(seeded.id, 1)
        assertEquals("cancelled", cancelled.reminder.status)
        assertEquals(2, cancelled.reminder.rev)
        assertTrue(repo.sync().deleted.actions.isEmpty())
        try { repo.cancelReminder(seeded.id, 1); fail("Expected conflict") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("版本冲突")) }
        try { repo.confirmReminder(seeded.id, 2); fail("Expected unavailable") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不可确认")) }
        repo.deleteReminder(seeded.id)
        try { repo.cancelReminder(seeded.id, 2); fail("Expected missing") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不存在")) }
    }
    @Test fun mockCancelDoneIsRejectedWithoutChanges() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val done = repo.seedReminder("吃药", "2099-01-01T00:00:00Z", status = "done")
        val before = repo.sync()
        try { repo.cancelReminder(done.id, done.rev); fail("Expected unavailable") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不可取消")) }
        val after = repo.sync()
        assertEquals(before, after)
    }
    @Test fun cancelUsesPostRevBearerAndReadableStateConflict() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"reminder":{"id":"r1","title":"吃药","status":"cancelled","rev":4},"action":{"id":"a2","type":"cancel_reminder","reminder_id":"r1"},"work_item_id":"w1","duplicate":false}""")
            val result = repo.cancelReminder("r1", 3)
            assertEquals("cancelled", result.reminder.status)
            assertEquals("cancel_reminder", result.action?.type)
            assertEquals("w1", result.work_item_id)
            assertFalse(result.duplicate)
            val request = server.takeRequest()
            assertEquals("POST", request.method)
            assertEquals("/v1/reminders/r1/cancel", request.path)
            assertEquals("Bearer access", request.getHeader("Authorization"))
            assertEquals("{\"rev\":3}", request.body.readUtf8())
            server.json("""{"error":{"code":"conflict","message":"提醒当前不可取消"}}""", 409)
            try { repo.cancelReminder("r1", 4); fail("Expected conflict") }
            catch (e: retrofit2.HttpException) {
                // 409 统一走 readableError 固定文案（与 patch/confirm 契约一致），不回显服务端 message。
                assertEquals(409, e.code()); assertEquals("版本冲突，请同步后重试", readableError(e))
            }
        }
    }
    @Test fun mockCompleteScheduledTombstonesActionAndIsIdempotent() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val seeded = repo.seedReminder("吃药", "2099-01-01T00:00:00Z")
        val confirmed = repo.confirmReminder(seeded.id, seeded.rev)
        val before = repo.sync()
        val done = repo.completeReminder(seeded.id, confirmed.reminder.rev)
        assertEquals("done", done.reminder.status)
        assertEquals(3, done.reminder.rev)
        assertFalse(done.duplicate)
        assertEquals("complete_reminder", done.action?.type)
        assertEquals(3, done.action?.payload?.get("rev"))
        val delta = repo.sync(before.cursor)
        assertEquals(listOf(confirmed.action!!.id), delta.deleted.actions)
        assertEquals(done.action, delta.actions.single())
        assertEquals(done.reminder, delta.reminders.single())
        val retry = repo.completeReminder(seeded.id, done.reminder.rev)
        assertTrue(retry.duplicate)
        assertEquals(done.action, retry.action)
        assertEquals(done.reminder, retry.reminder)
        assertEquals(delta.cursor, repo.sync(delta.cursor).cursor)
    }
    @Test fun mockCompleteSuggestedAndRejectStaleRev() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val seeded = repo.seedReminder("吃药", "2099-01-01T00:00:00Z")
        try { repo.completeReminder(seeded.id, 2); fail("Expected conflict") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("版本冲突")) }
        val done = repo.completeReminder(seeded.id, 1)
        assertEquals("done", done.reminder.status)
        assertEquals(2, done.reminder.rev)
        assertTrue(repo.sync().deleted.actions.isEmpty())
        try { repo.completeReminder(seeded.id, 1); fail("Expected conflict") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("版本冲突")) }
        try { repo.confirmReminder(seeded.id, 2); fail("Expected unavailable") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不可确认")) }
        repo.deleteReminder(seeded.id)
        try { repo.completeReminder(seeded.id, 2); fail("Expected missing") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不存在")) }
    }
    @Test fun mockCompleteCancelledIsRejectedWithoutChanges() = runBlocking {
        val repo = MockRepository(seedOnLogin = false); repo.login("demo")
        val done = repo.seedReminder("吃药", "2099-01-01T00:00:00Z", status = "cancelled")
        val before = repo.sync()
        try { repo.completeReminder(done.id, done.rev); fail("Expected unavailable") }
        catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不可完成")) }
        val after = repo.sync()
        assertEquals(before, after)
    }
    @Test fun completeUsesPostRevBearerAndReadableStateConflict() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"reminder":{"id":"r1","title":"吃药","status":"done","rev":4},"action":{"id":"a2","type":"complete_reminder","reminder_id":"r1"},"work_item_id":"w1","duplicate":false}""")
            val result = repo.completeReminder("r1", 3)
            assertEquals("done", result.reminder.status)
            assertEquals("complete_reminder", result.action?.type)
            assertEquals("w1", result.work_item_id)
            assertFalse(result.duplicate)
            val request = server.takeRequest()
            assertEquals("POST", request.method)
            assertEquals("/v1/reminders/r1/complete", request.path)
            assertEquals("Bearer access", request.getHeader("Authorization"))
            assertEquals("{\"rev\":3}", request.body.readUtf8())
            server.json("""{"error":{"code":"conflict","message":"提醒当前不可完成"}}""", 409)
            try { repo.completeReminder("r1", 4); fail("Expected conflict") }
            catch (e: retrofit2.HttpException) {
                // 409 统一走 readableError 固定文案（与 patch/confirm 契约一致），不回显服务端 message。
                assertEquals(409, e.code()); assertEquals("版本冲突，请同步后重试", readableError(e))
            }
        }
    }
    @Test fun mockLoginSyncReminderLinksLoadDemoNoteDetails() = runBlocking {
        val repo = MockRepository()
        repo.login("demo")
        val synced = repo.sync(0, 100)
        val demoIds = setOf("note-demo-1", "note-demo-2")
        assertEquals(2, synced.reminders.size)
        assertEquals(demoIds, synced.reminders.map { it.note_id }.toSet())
        assertEquals(demoIds, synced.notes.map { it.id }.toSet())
        val listedNotes = repo.notes().associateBy { it.id }
        assertEquals(demoIds, listedNotes.keys)
        for (reminder in synced.reminders) {
            val noteId = reminder.note_id
            if (!noteId.isNullOrBlank()) {
                val detail = repo.note(noteId)
                assertEquals(noteId, detail.id)
                assertEquals(reminder.title, detail.title)
                assertEquals(listedNotes[noteId], detail)
                assertEquals(synced.notes.single { it.id == noteId }, detail)
            }
        }
        assertEquals("note-demo-1", repo.search("绿植").single().note_id)
        assertEquals("note-demo-2", repo.search("周报").single().note_id)
    }

    @Test fun mockNoteDetailTracksDeletionAndSession() = runBlocking {
        val repo = MockRepository(organizeAfterReads = 0)
        repo.login("demo")
        val created = repo.create(noteDraft("详情正文", "detail-note"))
        assertEquals("详情正文", repo.note(created.note_id).text)
        for (id in listOf("missing", created.note_id)) {
            if (id == created.note_id) repo.delete(id)
            try { repo.note(id); fail("Expected missing note") }
            catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不存在")) }
        }
        repo.logout()
        try { repo.note(created.note_id); fail("Expected expired session") }
        catch (_: SessionExpired) { }
    }
    @Test fun noteDetailUsesGetAndBearerAndReadable404() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"id":"n1","text":"正文","status":"organized","rev":3,"title":"标题","recorded_at":"2026-11-03T00:00:00Z","tags":["标签"],"todos":[{"text":"待办","due":"明天"}],"category":"生活"}""")
            val note = repo.note("n1")
            assertEquals("n1", note.id); assertEquals("正文", note.text)
            assertEquals("organized", note.status); assertEquals(3, note.rev)
            assertEquals("标题", note.title); assertEquals(listOf("标签"), note.tags)
            assertEquals(listOf(NoteTodo("待办", "明天")), note.todos)
            assertEquals("生活", note.category)
            assertEquals("2026-11-03T00:00:00Z", note.recorded_at)
            val request = server.takeRequest()
            assertEquals("GET", request.method); assertEquals("/v1/notes/n1", request.path)
            assertEquals("Bearer access", request.getHeader("Authorization"))
            server.json("""{"error":{"code":"not_found","message":"记事不存在"}}""", 404)
            try { repo.note("missing"); fail("Expected HTTP error") }
            catch (e: retrofit2.HttpException) {
                assertEquals(404, e.code()); assertTrue(readableError(e).contains("不存在"))
            }
            server.takeRequest()
            server.json("{}", 404)
            try { repo.note("deleted"); fail("Expected HTTP error") }
            catch (e: retrofit2.HttpException) { assertTrue(readableError(e).contains("不存在")) }
        }
    }
    @Test fun noteDetailRejectsBlankIdWithoutNetwork() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val mock = MockRepository(); mock.login("demo")
            val real = RealRepository(realApi(server.url("/").toString()), Store())
            for (repo in listOf(mock, real)) {
                for (id in listOf("", "   ")) {
                    try { repo.note(id); fail("Expected invalid id") }
                    catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("不能为空")) }
                }
            }
            assertEquals(0, server.requestCount)
        }
    }

    @Test fun mockSearchTracksMemoriesAndSession() = runBlocking {
        val repo = MockRepository(organizeAfterReads = 0); repo.login("demo")
        assertTrue(repo.search("  绿植 ").isNotEmpty())
        repo.create(noteDraft("Kotlin 搜索记录", "search-note"))
        assertEquals("search-note", repo.search("kotlin").single().note_id)
        repo.organizeNote("search-note", "整理标题", emptyList(), emptyList())
        assertEquals("search-note", repo.search("整理标题").single().note_id)
        repo.create(noteDraft("KOTLIN 第二条", "second"))
        assertEquals(1, repo.search("kotlin", 1).size)
        repo.patch("search-note", 2, "修改后的记忆")
        assertTrue(repo.search("整理标题").isEmpty())
        assertEquals("search-note", repo.search("修改后").single().note_id)
        repo.delete("search-note")
        assertTrue(repo.search("修改后").isEmpty())
        repo.logout(); repo.login("demo")
        assertTrue(repo.search("kotlin").isEmpty())
        repo.logout()
        try { repo.search("绿植"); fail("Expected session error") } catch (_: SessionExpired) { }
    }
    @Test fun searchValidatesBeforeSendingRequests() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val mock = MockRepository(); mock.login("demo")
            val real = RealRepository(realApi(server.url("/").toString()), Store())
            for (repo in listOf(mock, real)) {
                for (query in listOf("", "   ", "字".repeat(501), "a\u0000")) {
                    try { repo.search(query); fail("Expected invalid query") }
                    catch (e: IllegalArgumentException) { assertTrue(readableError(e).contains("有效内容")) }
                }
                for (limit in listOf(0, 51)) {
                    try { repo.search("记忆", limit); fail("Expected invalid limit") }
                    catch (_: IllegalArgumentException) { }
                }
            }
            assertEquals(0, server.requestCount)
        }
    }
    @Test fun retrofitSearchContractAndRefresh() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("old", "rotation-1", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("{}", 401)
            server.json(tokens)
            server.json("""{"items":[{"memory_id":"m1","note_id":"n1","text":"记忆正文","score":0.85},
                {"memory_id":"m2","note_id":null,"text":"另一条","score":null}]}""")
            val hits = repo.search("  绿植 & Kotlin  ", 7)
            assertEquals(SearchHit("m1", "n1", "记忆正文", 0.85), hits.first())
            assertNull(hits[1].score); assertNull(hits[1].note_id)
            val first = server.takeRequest()
            assertEquals("GET", first.method)
            assertEquals("/v1/search", first.requestUrl!!.encodedPath)
            assertEquals("绿植 & Kotlin", first.requestUrl!!.queryParameter("q"))
            assertEquals("7", first.requestUrl!!.queryParameter("top_k"))
            assertEquals("Bearer old", first.getHeader("Authorization"))
            val refresh = server.takeRequest()
            assertEquals("/v1/auth/refresh", refresh.path)
            assertEquals("rotation-1", JsonParser.parseString(refresh.body.readUtf8()).asJsonObject["refresh_token"].asString)
            val retry = server.takeRequest()
            assertEquals(first.path, retry.path)
            assertEquals("Bearer access-1", retry.getHeader("Authorization"))
            server.json("""{"items":[]}""")
            assertTrue(repo.search("无结果").isEmpty())
            assertEquals("10", server.takeRequest().requestUrl!!.queryParameter("top_k"))
        }
    }
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
    @Test fun mockDeleteUnknownNoteIsANoOp() = runBlocking {
        val repo = MockRepository(seedOnLogin = false)
        repo.login("demo")
        repo.create(noteDraft("保留这条", "keep"))
        val before = repo.sync()
        repo.delete("missing")
        repo.delete("missing")
        assertEquals(before, repo.sync())
        assertEquals(listOf("keep"), repo.notes().map { it.id })
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
        assertEquals(listOf("写日记", "买牛奶", "周五整理周报，回顾本周工作", "每周给绿植浇水"),
            repo.notes().map { it.text })
        assertTrue(repo.ask("今天做什么？").answer.contains("本地演示"))
        repo.logout(); assertFalse(repo.signedIn)
        repo.login("another-demo")
        assertEquals(setOf("note-demo-1", "note-demo-2"), repo.notes().map { it.id }.toSet())
    }
    @Test fun mockRejectsEmptyAndOversizeInput() = runBlocking {
        val repo = MockRepository(); repo.login("demo")
        for (text in listOf(" ", "x".repeat(5001), "bad\u0000text")) {
            try { repo.create(noteDraft(text)); fail("Expected validation") } catch (_: IllegalArgumentException) { }
        }
        try { repo.ask("x".repeat(2001)); fail("Expected validation") } catch (_: IllegalArgumentException) { }
        assertEquals(setOf("note-demo-1", "note-demo-2"), repo.notes().map { it.id }.toSet())
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
    @Test fun askParsesNullableAndLinkedCitationNoteIds() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val store = Store().apply { tokens = Tokens("access", "refresh", 900, "u1") }
            val repo = RealRepository(realApi(server.url("/").toString()), store)
            server.json("""{"answer":"回答","citations":[{"memory_id":"m1","note_id":null,"text":"未关联正文"},{"memory_id":"m2","note_id":"n1","text":"关联正文"}]}""")
            val citations = repo.ask("记了什么").citations
            assertEquals(2, citations.size)
            assertNull(citations[0].note_id); assertEquals("未关联正文", citations[0].text)
            assertEquals("n1", citations[1].note_id); assertEquals("关联正文", citations[1].text)
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
