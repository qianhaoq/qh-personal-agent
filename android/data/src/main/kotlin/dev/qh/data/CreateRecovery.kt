package dev.qh.data

/** Only drafts and complete create requests are durable; these are never server projections. */
data class ComposerDraft(val text: String = "", val version: Long = 0, val mode: String = "note",
    val source: String = "app", val askOutcomeUnknown: Boolean = false)
data class CreateRecovery(val operationId: String, val request: NoteCreate,
    val state: String = "unknown", val discoveredNoteId: String? = null)
data class ComposerState(val draft: ComposerDraft = ComposerDraft(), val recovery: CreateRecovery? = null)

interface ComposerStore {
    fun load(owner: RequestOwner): ComposerState
    /** Must be atomic and durable. Exceptions forbid the caller from sending HTTP. */
    fun save(owner: RequestOwner, state: ComposerState)
    fun clear()
}

/** All local effects hold the same session authority lock as logout; no check/await/write gap. */
class CreateRecoveryFlow(private val repository: Repository, private val disk: ComposerStore) {
    fun load(owner: RequestOwner): ComposerState = repository.withCurrentOwner(owner) {
        disk.load(owner).let { state ->
            if (state.recovery?.state == "sending") state.copy(recovery = state.recovery.copy(state = "unknown"))
                .also { disk.save(owner, it) } else state
        }
    }
    fun edit(owner: RequestOwner, draft: ComposerDraft): ComposerState = repository.withCurrentOwner(owner) {
        disk.load(owner).copy(draft = draft).also { disk.save(owner, it) }
    }
    fun prepare(owner: RequestOwner, replaceLive: Boolean = false): CreateRecovery = repository.withCurrentOwner(owner) {
        val state = disk.load(owner)
        check(state.recovery == null || (replaceLive && state.recovery.state == "live" &&
            state.recovery.operationId != owner.operationId)) { "请先核对或移除原请求恢复项" }
        validText(state.draft.text, 5000)
        val recovery = CreateRecovery(owner.operationId,
            noteDraft(state.draft.text, source = state.draft.source), "sending")
        // B starts independently before HTTP; a response to A never consumes B.
        disk.save(owner, ComposerState(state.draft.copy(text = "", version = state.draft.version + 1), recovery))
        recovery
    }
    private fun update(owner: RequestOwner, recovery: CreateRecovery) = repository.withCurrentOwner(owner) {
        val current = disk.load(owner)
        check(current.recovery?.operationId == recovery.operationId && current.recovery.request == recovery.request) { "恢复项已变化" }
        disk.save(owner, current.copy(recovery = recovery))
    }
    suspend fun send(owner: RequestOwner, recovery: CreateRecovery): Note {
        update(owner, recovery.copy(state = "sending")) // durable before every send
        try {
            val captured = owner.copy(operationId = recovery.operationId)
            val created = repository.create(recovery.request, captured)
            update(owner, recovery.copy(discoveredNoteId = created.note_id))
            return authoritative(captured, recovery.copy(discoveredNoteId = created.note_id))
        } catch (error: Exception) {
            if (repository.isCurrent(owner)) repository.withCurrentOwner(owner) {
                val current = disk.load(owner).recovery
                if (current?.operationId == recovery.operationId) {
                    val state = if (error is retrofit2.HttpException && error.code() == 422) "rejected" else "unknown"
                    update(owner, current.copy(state = if (current.state == "deleted") "deleted" else state))
                }
            }
            throw error
        }
    }
    suspend fun reconcile(owner: RequestOwner): Note? {
        val recovery = load(owner).recovery ?: return null
        if (recovery.state == "deleted") return null
        // Never POST from reconcile. Absence from a live list is not proof of non-acceptance.
        val captured = owner.copy(operationId = recovery.operationId)
        val live = repository.notes(captured).find { it.client_note_id == recovery.request.client_note_id }
        val id = live?.id ?: recovery.discoveredNoteId ?: return null
        return authoritative(captured, recovery.copy(discoveredNoteId = id))
    }
    private suspend fun authoritative(owner: RequestOwner, recovery: CreateRecovery): Note {
        val note = try { repository.note(requireNotNull(recovery.discoveredNoteId), owner) }
        catch (error: retrofit2.HttpException) {
            if (error.code() == 404) update(owner, recovery.copy(state = "deleted"))
            throw error
        }
        check(note.client_note_id == recovery.request.client_note_id) { "服务器创建身份不匹配" }
        update(owner, recovery.copy(state = "live"))
        return note
    }
    suspend fun retryIdentical(owner: RequestOwner): Note? {
        val found = reconcile(owner)
        if (found != null) return found
        val recovery = load(owner).recovery ?: return null
        check(recovery.state != "deleted") { "原记事已删除，请明确另存草稿" }
        return send(owner, recovery)
    }
    fun markDeleted(owner: RequestOwner, serverId: String) = repository.withCurrentOwner(owner) {
        val state = disk.load(owner)
        state.recovery?.takeIf { it.discoveredNoteId == serverId }?.let {
            disk.save(owner, state.copy(recovery = it.copy(state = "deleted")))
        }
    }
    fun removeLocalRecovery(owner: RequestOwner) = repository.withCurrentOwner(owner) {
        val current = disk.load(owner)
        disk.save(owner, current.copy(recovery = null)) // preserve B, no server effects
    }
}
