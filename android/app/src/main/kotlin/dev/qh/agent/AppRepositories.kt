package dev.qh.agent

import android.content.Context
import dev.qh.data.RealRepository
import dev.qh.data.realApi
import dev.qh.security.SecureTokens
import java.util.concurrent.atomic.AtomicLong

/** Process-wide real repository and session generation fence for boot vs foreground. */
object AppRepositories {
    private val lock = Any()
    private val generation = AtomicLong(0)
    @Volatile private var real: RealRepository? = null

    fun sessionGeneration(): Long = generation.get()

    /** Invalidate in-flight boot side effects (e.g. on logout). */
    fun bumpSession(): Long = synchronized(lock) { generation.incrementAndGet() }

    /** Run [block] only if [expected] is still the current session generation. */
    fun ifCurrent(expected: Long, block: () -> Unit) = synchronized(lock) {
        if (generation.get() == expected) block()
    }

    fun real(context: Context): RealRepository {
        real?.let { return it }
        synchronized(this) {
            real?.let { return it }
            val app = context.applicationContext
            return RealRepository(realApi(BuildConfig.API_BASE_URL), SecureTokens(app)).also { real = it }
        }
    }
}
