package dev.qh.agent

import android.content.Context
import dev.qh.data.RealRepository
import dev.qh.data.realApi
import dev.qh.security.SecureTokens
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicLong

/** Process-wide real repository and session generation fence for boot vs foreground. */
object AppRepositories {
    private val lock = Any()
    @Volatile private var appContext: android.content.Context? = null
    private val generation = AtomicLong(0)
    private val alarmGeneration = AtomicLong(0)
    private val reminderAlarmGenerations = mutableMapOf<String, Long>()
    @Volatile private var real: RealRepository? = null

    private val remindersMutatedListeners = CopyOnWriteArrayList<(Long) -> Unit>()

    /** Listeners run synchronously under the session fence; enqueue work without blocking. */
    fun subscribeRemindersMutated(listener: (Long) -> Unit): () -> Unit {
        remindersMutatedListeners.add(listener)
        return { remindersMutatedListeners.remove(listener) }
    }

    fun notifyRemindersMutated() = synchronized(lock) {
        val session = generation.get()
        remindersMutatedListeners.forEach { it(session) }
    }

    fun sessionGeneration(): Long = generation.get()

    /** Invalidate in-flight boot side effects (e.g. on logout). */
    fun bumpSession(): Long = synchronized(lock) {
        appContext?.let { SecureTokens(it).bumpSessionEpoch() }
        generation.incrementAndGet()
    }

    /** Run [block] only if [expected] is still the current session generation. */
    fun ifCurrent(expected: Long, block: () -> Unit) = synchronized(lock) {
        if (generation.get() == expected) block()
    }

    fun alarmGeneration(): Long = alarmGeneration.get()

    /** Register captures so bulk invalidation also fences previously unseen reminders. */
    fun alarmGeneration(reminderId: String): Long = synchronized(lock) {
        reminderAlarmGenerations.getOrPut(reminderId) { 0L }
    }

    /** Invalidate stale sync responses and cancel deleted-note alarms under the consume lock. */
    fun invalidateAlarms(block: () -> Unit) = synchronized(lock) {
        alarmGeneration.incrementAndGet()
        reminderAlarmGenerations.replaceAll { _, value -> value + 1 }
        block()
    }

    /** A reminder mutation fences bulk sync without dropping other reminders' callbacks. */
    fun invalidateAlarms(reminderId: String, block: () -> Unit) = synchronized(lock) {
        reminderAlarmGenerations[reminderId] = (reminderAlarmGenerations[reminderId] ?: 0L) + 1
        alarmGeneration.incrementAndGet()
        block()
    }

    fun consumeIfCurrent(session: Long, reminderId: String, alarms: Long, block: () -> Unit) =
        synchronized(lock) {
            if (generation.get() == session &&
                (reminderAlarmGenerations[reminderId] ?: 0L) == alarms) block()
        }

    fun consumeIfCurrent(session: Long, alarms: Long, block: () -> Unit) = synchronized(lock) {
        if (generation.get() == session && alarmGeneration.get() == alarms) block()
    }

    /** A successful terminal mutation must clear local state even after a same-reminder snooze.
     * A non-null [reinstallAlarm] denotes snooze and still requires the captured alarm fence.
     * Keep session/sign-in validation and all effects under the same consume lock.
     */
    internal fun applyNotificationActionLocalEffects(
        session: Long, reminderId: String, alarms: Long, signedIn: () -> Boolean,
        cancelAlarm: () -> Unit, reinstallAlarm: (() -> Unit)?, dismissNotification: () -> Unit
    ) = synchronized(lock) {
        if (generation.get() != session || !signedIn()) return@synchronized
        if (reinstallAlarm != null &&
            (reminderAlarmGenerations[reminderId] ?: 0L) != alarms) return@synchronized
        invalidateAlarms(reminderId) {
            cancelAlarm()
            reinstallAlarm?.invoke()
        }
        dismissNotification()
        notifyRemindersMutated()
    }

    fun real(context: Context): RealRepository {
        real?.let { return it }
        synchronized(this) {
            real?.let { return it }
            val app = context.applicationContext
            appContext = app
            return RealRepository(realApi(BuildConfig.API_BASE_URL), SecureTokens(app)).also { real = it }
        }
    }
}
