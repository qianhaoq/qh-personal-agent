package dev.qh.agent

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class NotificationPromptTest {
    @Test fun explanationClaimIsDurableAcrossRecreationAndDoesNotRepromptAfterDismissal() {
        val context: Context = ApplicationProvider.getApplicationContext()
        context.getSharedPreferences("notification_explanation", Context.MODE_PRIVATE).edit().clear().commit()
        assertTrue(NotificationPrompt(context).claim())
        assertFalse(NotificationPrompt(context).claim())
        assertFalse(NotificationPrompt(context).claim())
    }
}
