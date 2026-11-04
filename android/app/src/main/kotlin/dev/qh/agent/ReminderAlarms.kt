package dev.qh.agent

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.widget.Toast
import dev.qh.data.ReminderConfirmResponse
import dev.qh.data.ReminderDto
import dev.qh.data.SyncAction
import dev.qh.data.SyncResponse
import dev.qh.security.SecureTokens
import java.time.Instant
import java.time.format.DateTimeParseException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject

private const val ALARM_PREFS = "pending_reminder_alarms"
private const val ALARM_ACTION = "dev.qh.agent.action.REMINDER_ALARM"
private const val EXTRA_REMINDER_ID = "reminder_id"
private const val EXTRA_TITLE = "title"
private const val NOTIFICATION_CHANNEL = "reminders"
// Soft budget for optional post-boot network sync (does not hold the broadcast).
private const val BOOT_SYNC_BUDGET_MS = 10_000L
private data class PendingReminder(val actionId: String, val reminderId: String,
    val title: String, val triggerAt: Long, val noteId: String? = null)

class ReminderAlarmScheduler(context: Context) {
    private val appContext = context.applicationContext
    private val alarms = appContext.getSystemService(AlarmManager::class.java)
    private val prefs = appContext.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE)

    fun consume(response: SyncResponse) {
        response.actions.filter { it.type == "schedule_reminder" }.forEach(::schedule)
        response.actions.filter { it.type in listOf("cancel_reminder", "complete_reminder") }.forEach { action ->
            (action.reminder_id ?: action.payload["reminder_id"] as? String)?.let(::cancelReminder)
        }
        response.deleted.notes.forEach(::cancelForNote)
        response.deleted.actions.forEach(::cancelAction)
        response.deleted.reminders.forEach(::cancelReminder)
        response.reminders.filter { it.status == "done" || it.status == "cancelled" }
            .forEach { cancelReminder(it.id) }
    }
    fun confirmed(response: ReminderConfirmResponse) {
        response.action?.let(::schedule) ?: schedule(response.reminder)
    }
    fun restore() = pending().forEach(::setAlarm)
    fun fired(reminderId: String) { cancelReminder(reminderId) }
    fun clear() {
        pending().forEach(::cancel)
        prefs.edit().clear().apply()
    }
    private fun schedule(action: SyncAction) {
        val reminderId = action.reminder_id ?: action.payload["reminder_id"] as? String ?: return
        if (reminderId.isBlank()) return
        val fireAt = action.payload["fire_at"] as? String ?: return
        val title = (action.payload["title"] as? String).orEmpty().ifBlank { "随记提醒" }
        val triggerAt = try { Instant.parse(fireAt).toEpochMilli() }
        catch (_: DateTimeParseException) { return }
        setAlarm(PendingReminder(action.id, reminderId, title, triggerAt,
            action.payload["note_id"] as? String))
    }
    private fun schedule(reminder: ReminderDto) {
        if (reminder.status != "scheduled") return
        val fireAt = reminder.fire_at ?: return
        val triggerAt = try { Instant.parse(fireAt).toEpochMilli() }
        catch (_: DateTimeParseException) { return }
        setAlarm(PendingReminder("", reminder.id, reminder.title.ifBlank { "随记提醒" }, triggerAt, reminder.note_id))
    }
    private fun setAlarm(reminder: PendingReminder) {
        val now = System.currentTimeMillis()
        // Historical sync actions must not create alarms, but a locally persisted pending
        // alarm may still be waiting for inexact/Doze delivery even after triggerAt.
        if (reminder.triggerAt <= now && !prefs.contains(key(reminder.reminderId))) return
        val triggerAt = maxOf(reminder.triggerAt, now)
        val payload = JSONObject()
            .put("action_id", reminder.actionId).put("reminder_id", reminder.reminderId)
            .put("title", reminder.title).put("trigger_at", reminder.triggerAt)
        reminder.noteId?.let { payload.put("note_id", it) }
        prefs.edit().putString(key(reminder.reminderId), payload.toString()).apply()
        val intent = alarmIntent(reminder)
        try {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || alarms.canScheduleExactAlarms()) {
                alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, intent)
            } else {
                alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, intent)
            }
        } catch (_: SecurityException) {
            // Exact-alarm access can be revoked between the check and scheduling.
            alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, intent)
        }
    }
    private fun cancelAction(actionId: String) = pending().filter { it.actionId == actionId }.forEach(::cancel)
    fun cancelForNote(noteId: String) = pending().filter { it.noteId == noteId }.forEach(::cancel)
    fun cancelReminder(reminderId: String) = pending().filter { it.reminderId == reminderId }.forEach(::cancel)
    private fun cancel(reminder: PendingReminder) {
        val intent = alarmIntent(reminder)
        alarms.cancel(intent)
        intent.cancel()
        prefs.edit().remove(key(reminder.reminderId)).apply()
    }
    private fun alarmIntent(reminder: PendingReminder): PendingIntent {
        val intent = Intent(appContext, ReminderAlarmReceiver::class.java).setAction(ALARM_ACTION)
            .setData(Uri.Builder().scheme("qh-agent").authority("reminder").appendPath(reminder.reminderId).build())
            .putExtra(EXTRA_REMINDER_ID, reminder.reminderId).putExtra(EXTRA_TITLE, reminder.title)
        return PendingIntent.getBroadcast(appContext, reminder.reminderId.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }
    private fun pending(): List<PendingReminder> = prefs.all.mapNotNull { (_, raw) ->
        val json = (raw as? String)?.let { runCatching { JSONObject(it) }.getOrNull() } ?: return@mapNotNull null
        val reminderId = json.optString("reminder_id")
        val triggerAt = json.optLong("trigger_at")
        if (reminderId.isBlank() || triggerAt <= 0) return@mapNotNull null
        PendingReminder(json.optString("action_id"), reminderId,
            json.optString("title").ifBlank { "随记提醒" }, triggerAt,
            json.optString("note_id").takeIf { it.isNotBlank() })
    }
    private fun key(reminderId: String) = "reminder:$reminderId"
}

class ReminderAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // Drop delivery if the user already signed out (session fence).
        if (SecureTokens(context.applicationContext).read() == null) return
        val reminderId = intent.getStringExtra(EXTRA_REMINDER_ID) ?: return
        val title = intent.getStringExtra(EXTRA_TITLE).orEmpty().ifBlank { "随记提醒" }
        ReminderAlarmScheduler(context).fired(reminderId)
        val notifications = context.getSystemService(NotificationManager::class.java)
        notifications.createNotificationChannel(NotificationChannel(NOTIFICATION_CHANNEL, "随记提醒",
            NotificationManager.IMPORTANCE_HIGH))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(context, title, Toast.LENGTH_LONG).show()
            return
        }
        val open = Intent(context, MainActivity::class.java)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .setData(Uri.Builder().scheme("qh-agent").authority("reminder").appendPath(reminderId).build())
        val contentIntent = PendingIntent.getActivity(context, reminderId.hashCode(), open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(context, NOTIFICATION_CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_alert).setContentTitle("随记提醒")
            .setContentText(title).setStyle(Notification.BigTextStyle().bigText(title))
            .setContentIntent(contentIntent).setAutoCancel(true).build()
        notifications.notify(reminderId.hashCode(), notification)
    }
}

class ReminderBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED || (BuildConfig.DEBUG && BuildConfig.USE_MOCK_API)) return
        val appContext = context.applicationContext
        val repository = AppRepositories.real(appContext)
        if (!repository.signedIn) return
        val scheduler = ReminderAlarmScheduler(appContext)
        // Bound broadcast work: restore local alarms immediately; never wait on network here.
        scheduler.restore()
        val generation = AppRepositories.sessionGeneration()
        val alarmGeneration = AppRepositories.alarmGeneration()
        // Network sync is best-effort background work outside the broadcast lifetime.
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                if (generation != AppRepositories.sessionGeneration()) return@launch
                if (!repository.signedIn) return@launch
                val response = withTimeoutOrNull(BOOT_SYNC_BUDGET_MS) { repository.sync() } ?: return@launch
                AppRepositories.consumeIfCurrent(generation, alarmGeneration) {
                    if (repository.signedIn) scheduler.consume(response)
                }
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                // A boot without connectivity must not discard alarms already persisted on device.
            }
        }
    }
}
