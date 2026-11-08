package dev.qh.data

/** Receipt for an already durably closed session, never a credential container. */
data class LogoutSession(val generation: String, val identity: SessionIdentity?)
