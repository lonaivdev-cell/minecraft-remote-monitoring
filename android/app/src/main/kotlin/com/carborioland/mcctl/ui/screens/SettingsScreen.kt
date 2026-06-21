package com.carborioland.mcctl.ui.screens

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.carborioland.mcctl.BuildConfig
import com.carborioland.mcctl.data.ConnState
import com.carborioland.mcctl.data.ConnectionProfile
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.push.PushScheduler
import com.carborioland.mcctl.ui.LocalMessenger
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McSwitchRow
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.util.Format
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(container: AppContainer) {
    val repo = container.repository
    val connState by repo.state.collectAsStateWithLifecycle()
    val profile by container.profileStore.profile.collectAsStateWithLifecycle(initialValue = null)
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    val messenger = LocalMessenger.current
    val context = LocalContext.current
    val identity = remember { container.secureStore.identity() }

    // Push-alert settings, seeded from the saved profile once it loads.
    var seeded by remember { mutableStateOf(false) }
    var pushEnabled by remember { mutableStateOf(false) }
    var ntfyServer by remember { mutableStateOf("https://ntfy.sh") }
    var ntfyTopic by remember { mutableStateOf("") }
    LaunchedEffect(profile) {
        val p = profile
        if (!seeded && p != null) {
            pushEnabled = p.pushEnabled; ntfyServer = p.ntfyServer; ntfyTopic = p.ntfyTopic
            seeded = true
        }
    }
    val notifPermLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    fun ensureNotifPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    fun savePush() {
        val updated = (profile ?: ConnectionProfile()).copy(
            pushEnabled = pushEnabled, ntfyServer = ntfyServer.trim(), ntfyTopic = ntfyTopic.trim(),
        )
        scope.launch {
            container.profileStore.save(updated)
            PushScheduler.apply(context, updated)
            if (updated.pushReady) PushScheduler.pollNow(context)
            messenger(if (updated.pushReady) "Push alerts on" else "Push alerts off")
        }
    }

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
            SectionLabel("Push alerts")
            Text(
                "Subscribe to the box's ntfy topic and get watchdog alerts as notifications — " +
                    "no Firebase, it polls the same topic the server's `ntfy_*` sink publishes to " +
                    "(checked roughly every 15 min in the background).",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            )
            McSwitchRow(
                title = "Background alerts",
                checked = pushEnabled,
                onCheckedChange = { pushEnabled = it; if (it) ensureNotifPermission() },
                subtitle = "Topic must match the box's [notify].ntfy_topic.",
            )
            McTextField("ntfy server", ntfyServer, { ntfyServer = it }, modifier = Modifier.padding(top = 6.dp))
            McTextField("ntfy topic", ntfyTopic, { ntfyTopic = it }, modifier = Modifier.padding(top = 6.dp))
            Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                McButton("Save", kind = BtnKind.Primary, onClick = { savePush() })
                McButton("Test now", kind = BtnKind.Neutral, onClick = {
                    PushScheduler.pollNow(context); messenger("Polling ntfy…")
                })
            }
        }

        McPanel {
            SectionLabel("Offline assets")
            var usage by remember { mutableStateOf<Pair<Int, Long>?>(null) }
            var refresh by remember { mutableIntStateOf(0) }
            LaunchedEffect(refresh) { usage = container.iconCache.diskUsage() }
            val u = usage
            Text(
                when {
                    u == null -> "Measuring…"
                    u.first == 0 -> "No item icons cached yet. Use “Download all icons for offline” on the Items screen."
                    else -> "${u.first} item icons cached · ${Format.bytes(u.second)}"
                },
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim,
            )
            if (u != null && u.first > 0) {
                McButton("Clear cached assets", kind = BtnKind.Danger, modifier = Modifier.padding(top = 10.dp), onClick = {
                    scope.launch {
                        container.iconCache.clearOffline()
                        messenger("Cleared cached item assets")
                        refresh++
                    }
                })
            }
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
