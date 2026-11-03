package dev.qh.data

const val ORGANIZE_POLL_MAX_ATTEMPTS = 20
const val ORGANIZE_POLL_TIMEOUT_MS = 30_000L

// A missing note is still pending (e.g. not visible in the first list response).
fun shouldContinueOrganizePoll(status: String?, attempt: Int, elapsedMs: Long,
    maxAttempts: Int = ORGANIZE_POLL_MAX_ATTEMPTS,
    timeoutMs: Long = ORGANIZE_POLL_TIMEOUT_MS): Boolean =
    (status == null || status == "uploaded") && attempt < maxAttempts && elapsedMs < timeoutMs

fun nextOrganizePollDelayMs(@Suppress("UNUSED_PARAMETER") attempt: Int): Long = 1_500L

fun requireSessionAfterOrganizePoll(generationMatches: Boolean, signedIn: Boolean) {
    if (generationMatches && !signedIn) throw SessionExpired()
}
