package dev.qh.agent

import androidx.compose.runtime.Composable
import dev.qh.data.ReminderDto

@Composable
internal fun FlowReminderCard(model: AgentModel, reminder: ReminderDto,
    open: (String) -> Unit, terminal: (ReminderAction, ReminderDto) -> Unit) {
    ReminderCard(reminder, model.localSchedules[reminder.id] ?: LocalScheduleResult.UNKNOWN, model.busy,
        { model.retryLocal(reminder) }, open, { model.beginConfirm(reminder) },
        { model.snoozeReminder(reminder) }, terminal)
}
