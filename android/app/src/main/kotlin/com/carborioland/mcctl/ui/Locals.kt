package com.carborioland.mcctl.ui

import androidx.compose.runtime.staticCompositionLocalOf

/** Show a transient message (snackbar). Provided at the app root. */
val LocalMessenger = staticCompositionLocalOf<(String) -> Unit> { {} }

/**
 * Authorize a state-changing action, returning true to proceed. Backed by a biometric/
 * device-credential prompt when the profile asks for it; a no-op (always true) otherwise.
 * Read-only screens never call this.
 */
val LocalActionGate = staticCompositionLocalOf<suspend (String) -> Boolean> { { true } }
