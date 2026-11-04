package dev.qh.agent

import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderActionConfirmTest {
    @Test fun completeExplainsTheStateChange() {
        assertEquals("完成提醒？", reminderActionConfirmTitle(ReminderAction.COMPLETE))
        assertEquals("确认后将标记「买牛奶」为已完成。", reminderActionConfirmMessage(ReminderAction.COMPLETE, "买牛奶"))
    }

    @Test fun cancelExplainsTheStateChange() {
        assertEquals("取消提醒？", reminderActionConfirmTitle(ReminderAction.CANCEL))
        assertEquals("确认后将取消提醒「交水费」。", reminderActionConfirmMessage(ReminderAction.CANCEL, "交水费"))
    }

    @Test fun blankTitlesUseTheNotificationFallback() {
        for (title in listOf("", " ", "\t\n　")) {
            assertEquals("确认后将标记「随记提醒」为已完成。",
                reminderActionConfirmMessage(ReminderAction.COMPLETE, title))
            assertEquals("确认后将取消提醒「随记提醒」。",
                reminderActionConfirmMessage(ReminderAction.CANCEL, title))
        }
    }

    @Test fun missingNotificationTitleFallsBackAndNonBlankTitleIsPreserved() {
        assertEquals("随记提醒", reminderConfirmDisplayTitle(null))
        assertEquals(" 买牛奶 ", reminderConfirmDisplayTitle(" 买牛奶 "))
    }

    @Test fun busyBlocksConfirmationAndDismissal() {
        assertEquals(false, shouldAcceptReminderActionConfirm(busy = true))
        assertEquals(true, shouldAcceptReminderActionConfirm(busy = false))
    }
}
