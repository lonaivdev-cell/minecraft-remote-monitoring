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
    /** Poll the box's ntfy topic in the background and raise watchdog alerts as notifications. */
    val pushEnabled: Boolean = false,
    /** ntfy server the box publishes to (the v0.5.0 `ntfy_*` sink). */
    val ntfyServer: String = "https://ntfy.sh",
    /** ntfy topic to subscribe to (must match the box's `[notify].ntfy_topic`). */
    val ntfyTopic: String = "",
) {
    val isConfigured: Boolean get() = host.isNotBlank() && user.isNotBlank()
    val label: String get() = if (isConfigured) "$user@$host" else "not configured"

    /** Push can run only once a topic is set and the user has opted in. */
    val pushReady: Boolean get() = pushEnabled && ntfyTopic.isNotBlank() && ntfyServer.isNotBlank()
}
