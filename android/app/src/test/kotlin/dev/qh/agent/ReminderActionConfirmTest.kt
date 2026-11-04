package dev.qh.agent

import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderActionConfirmTest {
    @Test fun completeExplainsTheStateChange() {
        assertEquals("完成提醒？", reminderActionConfirmTitle(ReminderAction.COMPLETE))
        assertEquals("确认后将标记为已完成。", reminderActionConfirmMessage(ReminderAction.COMPLETE))
    }

    @Test fun cancelExplainsTheStateChange() {
        assertEquals("取消提醒？", reminderActionConfirmTitle(ReminderAction.CANCEL))
        assertEquals("确认后将取消该提醒。", reminderActionConfirmMessage(ReminderAction.CANCEL))
    }

    @Test fun busyBlocksConfirmationAndDismissal() {
        assertEquals(false, shouldAcceptReminderActionConfirm(busy = true))
        assertEquals(true, shouldAcceptReminderActionConfirm(busy = false))
    }
}
