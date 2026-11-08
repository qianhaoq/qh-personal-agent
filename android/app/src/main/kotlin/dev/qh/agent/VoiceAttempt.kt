package dev.qh.agent

import dev.qh.data.RequestOwner
import java.util.UUID

data class VoiceAttempt(val owner: RequestOwner, val mode: String, val draftVersion: Long,
    val id: String = UUID.randomUUID().toString()) {
    fun accepts(currentOwner: Boolean, currentMode: String, currentVersion: Long) =
        currentOwner && mode == currentMode && draftVersion == currentVersion
}
