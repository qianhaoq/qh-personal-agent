package dev.qh.agent

internal fun confirmationSessionCurrent(expected: String, current: String, signedIn: Boolean) =
    signedIn && expected == current

/** Main-thread state retained across Activity replacement; a submitted request is never replayed. */
internal class NotificationConfirmation {
    var submitted = false
        private set
    var completed = false
        private set
    val busy get() = submitted && !completed
    val canDismiss get() = !busy
    private var owner: Any? = null
    private var finish: (() -> Unit)? = null

    fun attach(owner: Any, finish: () -> Unit) {
        this.owner = owner
        this.finish = finish
        if (completed) finish()
    }

    fun detach(owner: Any) {
        if (this.owner === owner) {
            this.owner = null
            finish = null
        }
    }

    fun submit(sessionCurrent: Boolean, execute: (() -> Unit) -> Unit): Boolean {
        if (submitted || !sessionCurrent) return false
        submitted = true
        execute { complete() }
        return true
    }

    private fun complete() {
        completed = true
        finish?.invoke()
    }
}
