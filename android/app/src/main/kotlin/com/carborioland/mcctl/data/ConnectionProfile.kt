package com.carborioland.mcctl.data

import kotlinx.serialization.Serializable

/**
 * A saved connection to a CarborioLand box. Secrets are NOT here — the device key seed
 * lives in [com.carborioland.mcctl.data.security.SecureStore] (encrypted), and host
 * fingerprints in the known-hosts store. This is just the non-secret target + preferences.
 */
@Serializable
data class ConnectionProfile(
    val host: String = "",
    val port: Int = 22,
    val user: String = "ubuntu",
    val agentCommand: String = "mcctl agent",
    /** Request the `actions` capability (start/stop/backup/console/players) at handshake. */
    val enableActions: Boolean = true,
    /** Request the `destructive` capability (kill/restore/props.set/jvm.heap/ban). */
    val enableDestructive: Boolean = false,
    /** Require a biometric/device-credential unlock before any action runs. */
    val biometricForActions: Boolean = true,
) {
    val isConfigured: Boolean get() = host.isNotBlank() && user.isNotBlank()
    val label: String get() = if (isConfigured) "$user@$host" else "not configured"
}
