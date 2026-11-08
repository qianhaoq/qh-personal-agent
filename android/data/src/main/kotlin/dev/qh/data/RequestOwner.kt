package dev.qh.data

import java.io.IOException
import java.util.UUID

/** Metadata only. Reading this must never load access or refresh credentials. */
data class SessionIdentity(val userId: String, val loginEpoch: String, val closed: Boolean = false)
data class RequestOwner(val runtimeKey: String, val userId: String, val loginEpoch: String,
    val processGeneration: String, val operationId: String = UUID.randomUUID().toString())
class StaleRequestOwner : IOException("会话已变化，请重新提交")
