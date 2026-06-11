package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.carborioland.mcctl.data.ConnectionProfile
import com.carborioland.mcctl.data.ServerRepository
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.LocalMessenger
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McSwitchRow
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberVm
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class ConnectViewModel(private val container: AppContainer) : ViewModel() {
    private val repo: ServerRepository = container.repository

    var profile by mutableStateOf(ConnectionProfile())
        private set
    var publicKey by mutableStateOf(container.secureStore.identity().openSshPublicKey())
        private set
    var fingerprint by mutableStateOf(container.secureStore.identity().fingerprint())
        private set
    var connecting by mutableStateOf(false)
        private set
    var error by mutableStateOf<String?>(null)

    init {
        viewModelScope.launch { profile = container.profileStore.profile.first() }
    }

    fun update(block: (ConnectionProfile) -> ConnectionProfile) {
        profile = block(profile)
    }

    fun regenerateKey() {
        val id = container.secureStore.regenerateIdentity()
        publicKey = id.openSshPublicKey()
        fingerprint = id.fingerprint()
    }

    fun connect(onConnected: () -> Unit, onMessage: (String) -> Unit) {
        if (connecting) return
        error = null
        connecting = true
        viewModelScope.launch {
            container.profileStore.save(profile)
            repo.connect(profile)
                .onSuccess {
                    connecting = false
                    onMessage("Connected · mcctl ${it.mcctlVersion}")
                    onConnected()
                }
                .onFailure {
                    connecting = false
                    error = it.message ?: "connection failed"
                }
        }
    }
}

@Composable
fun ConnectScreen(container: AppContainer, onConnected: () -> Unit) {
    val vm = rememberVm { ConnectViewModel(container) }
    val clipboard = LocalClipboardManager.current
    val messenger = LocalMessenger.current
    val p = vm.profile

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        McPanel {
            SectionLabel("Device key")
            Text(
                "This phone has its own Ed25519 key. Authorize it on the box once — append the " +
                    "line below to ~/.ssh/authorized_keys for the server user. No password is ever stored.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.mc.dim,
            )
            Spacer(Modifier.padding(4.dp))
            Text(
                vm.publicKey,
                style = TerminalTextStyle,
                color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 8.dp),
            )
            Text("Fingerprint: ${vm.fingerprint}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            Row(Modifier.padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                McButton("Copy key", kind = BtnKind.Primary, onClick = {
                    clipboard.setText(AnnotatedString(vm.publicKey))
                    messenger("Public key copied")
                })
                McButton("New key", kind = BtnKind.Neutral, onClick = {
                    vm.regenerateKey()
                    messenger("New device key — re-authorize it on the server")
                })
            }
        }

        McPanel {
            SectionLabel("Server")
            McTextField("Host", p.host, { v -> vm.update { it.copy(host = v.trim()) } }, modifier = Modifier.padding(top = 6.dp))
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                McTextField("User", p.user, { v -> vm.update { it.copy(user = v.trim()) } }, modifier = Modifier.weight(2f))
                McTextField("Port", p.port.toString(), { v -> vm.update { it.copy(port = v.toIntOrNull() ?: it.port) } }, numeric = true, modifier = Modifier.weight(1f))
            }
            McTextField("Agent command", p.agentCommand, { v -> vm.update { it.copy(agentCommand = v) } }, modifier = Modifier.padding(top = 8.dp))
        }

        McPanel {
            SectionLabel("Capabilities & safety")
            McSwitchRow(
                "Allow actions", p.enableActions,
                { v -> vm.update { it.copy(enableActions = v) } },
                subtitle = "start/stop/restart, console, players, backups",
            )
            McSwitchRow(
                "Allow destructive", p.enableDestructive,
                { v -> vm.update { it.copy(enableDestructive = v) } },
                subtitle = "kill, restore, props.set, jvm.heap, ban — each still needs a typed confirm",
            )
            McSwitchRow(
                "Biometric for actions", p.biometricForActions,
                { v -> vm.update { it.copy(biometricForActions = v) } },
                subtitle = "Require fingerprint / PIN before any state change",
            )
        }

        vm.error?.let {
            McPanel { Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.danger) }
        }

        McButton(
            text = if (vm.connecting) "Connecting…" else "Connect",
            kind = BtnKind.Primary,
            large = true,
            enabled = p.isConfigured && !vm.connecting,
            onClick = { vm.connect(onConnected, messenger) },
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.padding(8.dp))
    }
}
