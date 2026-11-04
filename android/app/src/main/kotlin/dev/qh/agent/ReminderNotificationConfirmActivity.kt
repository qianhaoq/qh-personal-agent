package dev.qh.agent

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme

/** Notification PendingIntent opens UI directly; no broadcast-to-Activity trampoline. */
class ReminderNotificationConfirmActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val action = ReminderNotificationAction.fromIntentAction(intent.action)
        val confirmAction = action?.confirmAction
        val id = intent.getStringExtra(EXTRA_REMINDER_ID)?.takeIf { it.isNotBlank() }
        if (action == null || confirmAction == null || id == null) {
            finish()
            return
        }
        val session = savedInstanceState?.getLong("session") ?: AppRepositories.sessionGeneration()
        confirmationSession = session
        var submitted = false
        setContent {
            MaterialTheme {
                ReminderActionConfirmDialog(confirmAction, busy = false,
                    onDismiss = { finish() },
                    onConfirm = {
                        if (!submitted && session == AppRepositories.sessionGeneration()) {
                            submitted = true
                            executeReminderNotificationAction(this, action, id, confirmed = true)
                        }
                        finish()
                    })
            }
        }
    }

    private var confirmationSession = 0L

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putLong("session", confirmationSession)
        super.onSaveInstanceState(outState)
    }
}
