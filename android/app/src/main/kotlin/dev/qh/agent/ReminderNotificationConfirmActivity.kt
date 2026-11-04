package dev.qh.agent

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import dev.qh.security.SecureTokens

class NotificationConfirmationModel : ViewModel() {
    internal val request = NotificationConfirmation()
}

/** Notification PendingIntent opens UI directly; no broadcast-to-Activity trampoline. */
class ReminderNotificationConfirmActivity : ComponentActivity() {
    private var confirmationSession = ""
    private var request: NotificationConfirmation? = null
    private val busy = mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val action = ReminderNotificationAction.fromIntentAction(intent.action)
        val confirmAction = action?.confirmAction
        val id = intent.getStringExtra(EXTRA_REMINDER_ID)?.takeIf { it.isNotBlank() }
        if (action == null || confirmAction == null || id == null) {
            finish()
            return
        }
        // Wire process-wide token store so later bumpSession can rotate the durable epoch.
        AppRepositories.real(applicationContext)
        val tokens = SecureTokens(applicationContext)
        confirmationSession = savedInstanceState?.getString("sessionEpoch") ?: tokens.sessionEpoch()
        val state = ViewModelProvider(this)[NotificationConfirmationModel::class.java].request
        request = state
        // Process death loses the running job. Close without replaying an ambiguous server mutation.
        if (savedInstanceState?.getBoolean("submitted") == true && !state.submitted) {
            finish()
            return
        }
        state.attach(this) { finish() }
        busy.value = state.busy
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (state.canDismiss) finish() }
        })
        setContent {
            MaterialTheme {
                ReminderActionConfirmDialog(confirmAction, busy = busy.value,
                    onDismiss = { if (state.canDismiss) finish() },
                    onConfirm = {
                        val current = confirmationSessionCurrent(confirmationSession,
                            tokens.sessionEpoch(), tokens.read() != null)
                        val app = applicationContext
                        val accepted = state.submit(current) { done ->
                            busy.value = true
                            val main = Handler(Looper.getMainLooper())
                            executeReminderNotificationAction(app, action, id, confirmed = true) {
                                main.post { done() }
                            }
                        }
                        if (!accepted && !state.submitted) finish()
                    })
            }
        }
    }

    override fun onDestroy() {
        request?.detach(this)
        super.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("sessionEpoch", confirmationSession)
        outState.putBoolean("submitted", request?.submitted == true)
        super.onSaveInstanceState(outState)
    }
}
