package com.carborioland.mcctl.ui

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.CompletableDeferred

/**
 * Biometric/device-credential gate for actions. Returns true if the user authenticates
 * (or if the device has no secure lock to enforce — we don't block a phone with no
 * screen lock, the SSH key is still the real credential). Read-only status never calls
 * this; only state-changing actions do, matching the security model in TODO.md.
 */
object BiometricGate {

    private const val ALLOWED =
        BiometricManager.Authenticators.BIOMETRIC_WEAK or
            BiometricManager.Authenticators.DEVICE_CREDENTIAL

    fun isAvailable(activity: FragmentActivity): Boolean =
        BiometricManager.from(activity).canAuthenticate(ALLOWED) == BiometricManager.BIOMETRIC_SUCCESS

    suspend fun authenticate(
        activity: FragmentActivity,
        title: String,
        subtitle: String,
    ): Boolean {
        if (BiometricManager.from(activity).canAuthenticate(ALLOWED) != BiometricManager.BIOMETRIC_SUCCESS) {
            // No enrolled biometric/credential — don't lock the user out of their own server.
            return true
        }
        val result = CompletableDeferred<Boolean>()
        val executor = ContextCompat.getMainExecutor(activity)
        val prompt = BiometricPrompt(
            activity,
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(r: BiometricPrompt.AuthenticationResult) {
                    if (!result.isCompleted) result.complete(true)
                }

                override fun onAuthenticationError(code: Int, msg: CharSequence) {
                    if (!result.isCompleted) result.complete(false)
                }
            },
        )
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle(subtitle)
            .setAllowedAuthenticators(ALLOWED)
            .build()
        prompt.authenticate(info)
        return result.await()
    }
}
