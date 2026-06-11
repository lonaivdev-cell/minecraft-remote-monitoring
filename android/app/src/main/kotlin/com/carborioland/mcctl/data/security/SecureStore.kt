package com.carborioland.mcctl.data.security

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.carborioland.mcctl.core.ssh.Ed25519Identity
import com.carborioland.mcctl.core.ssh.KnownHostsStore

/**
 * The phone's secret vault, backed by [EncryptedSharedPreferences] (keys held in the
 * Android Keystore). It holds two things: the Ed25519 device-key seed and the trusted
 * host fingerprints. The seed never leaves the device; only the *public* key is shown to
 * the user to authorize on the server.
 */
class SecureStore(context: Context) : KnownHostsStore {

    private val prefs: SharedPreferences = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "mcctl_secure",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    fun hasIdentity(): Boolean = prefs.contains(KEY_SEED)

    /** The device identity, generating and persisting one on first use. */
    fun identity(): Ed25519Identity {
        prefs.getString(KEY_SEED, null)?.let { return Ed25519Identity.fromSeedB64(it) }
        return Ed25519Identity.generate().also { prefs.edit().putString(KEY_SEED, it.seedBase64()).apply() }
    }

    /** Rotate the device key (the user must re-authorize the new public key on the box). */
    fun regenerateIdentity(): Ed25519Identity =
        Ed25519Identity.generate().also { prefs.edit().putString(KEY_SEED, it.seedBase64()).apply() }

    // --- KnownHostsStore (TOFU) ---

    override fun fingerprintFor(host: String, port: Int): String? =
        prefs.getString(hostKey(host, port), null)

    override fun remember(host: String, port: Int, fingerprint: String) {
        prefs.edit().putString(hostKey(host, port), fingerprint).apply()
    }

    fun forgetHost(host: String, port: Int) {
        prefs.edit().remove(hostKey(host, port)).apply()
    }

    private fun hostKey(host: String, port: Int) = "hk:$host:$port"

    private companion object {
        const val KEY_SEED = "device_seed_b64"
    }
}
