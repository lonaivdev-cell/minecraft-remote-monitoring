package com.carborioland.mcctl

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.fragment.app.FragmentActivity
import com.carborioland.mcctl.ui.nav.McctlAppRoot
import com.carborioland.mcctl.ui.theme.McctlTheme

/**
 * The single activity. Extends [FragmentActivity] because androidx.biometric's
 * BiometricPrompt requires one; Compose's `setContent` is happy with any ComponentActivity.
 */
class MainActivity : FragmentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        val container = (application as McctlApp).container
        setContent {
            McctlTheme {
                McctlAppRoot(container)
            }
        }
    }
}
