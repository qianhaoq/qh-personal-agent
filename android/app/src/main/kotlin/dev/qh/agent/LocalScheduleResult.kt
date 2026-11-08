package dev.qh.agent

/** A system call returning successfully is a submission, never a delivery guarantee. */
enum class LocalScheduleResult(val label: String) {
    NOT_APPLICABLE("尚未确认"), UNKNOWN("本机排程待核对"),
    SUBMITTED_EXACT("已提交精确系统排程"), SUBMITTED_INEXACT("已提交非精确系统排程"),
    FAILED("本机排程失败"), EXPIRED("时间已过，未提交新排程");
    val submitted get() = this == SUBMITTED_EXACT || this == SUBMITTED_INEXACT
}

internal fun submitLocalAlarm(expired: Boolean, exactAllowed: () -> Boolean,
    exact: () -> Unit, inexact: () -> Unit): LocalScheduleResult {
    if (expired) return LocalScheduleResult.EXPIRED
    return try {
        if (exactAllowed()) {
            try { exact(); LocalScheduleResult.SUBMITTED_EXACT }
            catch (_: SecurityException) { inexact(); LocalScheduleResult.SUBMITTED_INEXACT }
        } else { inexact(); LocalScheduleResult.SUBMITTED_INEXACT }
    } catch (_: Exception) { LocalScheduleResult.FAILED }
}

data class ReminderStepResult(val local: LocalScheduleResult, val refreshFailed: Boolean)

/** Once confirm returns, later failures cannot be reported as a failed server mutation. */
internal suspend fun confirmReminderSteps(
    confirm: suspend () -> dev.qh.data.ReminderConfirmResponse,
    schedule: (dev.qh.data.ReminderConfirmResponse) -> LocalScheduleResult,
    apply: (dev.qh.data.ReminderDto, LocalScheduleResult) -> Unit,
    refresh: suspend () -> Unit
): ReminderStepResult {
    val response = confirm()
    val local = try { schedule(response) } catch (_: Exception) { LocalScheduleResult.FAILED }
    apply(response.reminder, local)
    val refreshFailed = try { refresh(); false }
        catch (e: kotlinx.coroutines.CancellationException) { throw e }
        catch (_: Exception) { true }
    return ReminderStepResult(local, refreshFailed)
}
