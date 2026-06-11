package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.carborioland.mcctl.BuildConfig
import com.carborioland.mcctl.data.ConnState
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.LocalMessenger
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(container: AppContainer) {
    val repo = container.repository
    val connState by repo.state.collectAsStateWithLifecycle()
    val profile by container.profileStore.profile.collectAsStateWithLifecycle(initialValue = null)
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    val messenger = LocalMessenger.current
    val identity = remember { container.secureStore.identity() }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        McPanel {
            SectionLabel("Session")
            KeyValue("Target", profile?.label ?: "—")
            KeyValue(
                "Connection",
                when (val s = connState) {
                    is ConnState.Connected -> "live · mcctl ${s.hello.mcctlVersion} · protocol ${s.hello.protocol}"
                    ConnState.Connecting -> "connecting…"
                    is ConnState.Failed -> "failed: ${s.message}"
                    ConnState.Disconnected -> "disconnected"
                },
            )
            val caps = repo.capabilities.joinToString(", ") { it.wire }.ifBlank { "read-only" }
            KeyValue("Capabilities", caps)
            if (connState is ConnState.Connected) {
                McButton("Disconnect", kind = BtnKind.Danger, modifier = Modifier.padding(top = 10.dp), onClick = {
                    scope.launch { repo.disconnect(); messenger("Disconnected") }
                })
            }
        }

        McPanel {
            SectionLabel("Device key")
            Text("Fingerprint of the Ed25519 key authorized on the box:", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            Text(identity.fingerprint(), style = TerminalTextStyle, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.padding(vertical = 6.dp))
            McButton("Copy public key", kind = BtnKind.Neutral, onClick = {
                clipboard.setText(AnnotatedString(identity.openSshPublicKey()))
                messenger("Public key copied")
            })
        }

        McPanel {
            SectionLabel("About")
            Text("mcctl — Minecraft Remote Control", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface)
            Text(
                "A thin client over `mcctl agent` (JSON-RPC 2.0 over SSH stdio): the tested Python core " +
                    "on the box is the one brain, this app is one of its faces. No open ports, no stored " +
                    "passwords — just your device's SSH key.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 4.dp),
            )
            KeyValue("App version", BuildConfig.VERSION_NAME)
            Text(
                "Fonts: Press Start 2P, VT323, Silkscreen — all SIL OFL (licenses bundled in assets).",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 8.dp),
            )
        }
    }
}
