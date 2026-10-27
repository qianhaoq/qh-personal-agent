package dev.qh.security

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import dev.qh.data.TokenStore
import dev.qh.data.Tokens
import java.util.UUID

class SecureTokens(context: Context) : TokenStore {
    private val prefs = EncryptedSharedPreferences.create(context, "session",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)
    @Synchronized override fun read(): Tokens? {
        val access = prefs.getString("access", null) ?: return null
        val refresh = prefs.getString("refresh", null) ?: return null
        return Tokens(access, refresh, prefs.getLong("ttl", 0), prefs.getString("user", "")!!)
    }
    @Synchronized override fun write(tokens: Tokens?) {
        val edit = prefs.edit()
        if (tokens == null) listOf("access", "refresh", "ttl", "user").forEach { edit.remove(it) }
        else edit.putString("access", tokens.access_token).putString("refresh", tokens.refresh_token)
            .putLong("ttl", tokens.expires_in).putString("user", tokens.user_id)
        check(edit.commit()) { "无法保存安全会话" }
    }
    // Kept across logout; never changes unless app data is explicitly cleared/uninstalled.
    @Synchronized override fun deviceId(): String = prefs.getString("device", null) ?: UUID.randomUUID().toString().also {
        check(prefs.edit().putString("device", it).commit()) { "无法保存设备标识" }
    }
}
