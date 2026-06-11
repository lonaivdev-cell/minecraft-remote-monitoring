package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.BackupEntry
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.ConfirmDialog
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McSwitchRow
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.util.Format

@Composable
fun BackupsScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.backupList() }
    val runner = rememberActionRunner(container)
    var full by remember { mutableStateOf(false) }
    var restore by remember { mutableStateOf<BackupEntry?>(null) }
    val canDestroy = Capability.DESTRUCTIVE in container.repository.capabilities

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            McSwitchRow("Full instance", full, { full = it }, subtitle = "Whole server dir, not just the world")
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                McButton("New backup", kind = BtnKind.Primary, enabled = !runner.busy, modifier = Modifier.weight(1f), onClick = {
                    runner.run("Create backup", refreshAfter = false, onComplete = res.reload) { c ->
                        c.backupCreate(full)?.let { "Backup: ${it.name} (${Format.bytes(it.size)})" } ?: "Backup created"
                    }
                })
                McButton("Prune", enabled = !runner.busy, modifier = Modifier.weight(1f), onClick = {
                    runner.run("Prune backups", refreshAfter = false, onComplete = res.reload) { c ->
                        val (_, removed) = c.backupPrune(); "Rotated out ${removed.size}"
                    }
                })
            }
            Text(
                "Snapshots are consistent while live (save-off → flush → tar → verify → save-on) and rotated GFS-style.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 6.dp),
            )
        }

        AsyncContent(res.state, onRetry = res.reload) { backups ->
            if (backups.isEmpty()) {
                EmptyHint("No backups yet — tap New backup.")
            } else {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(backups) { b ->
                        BackupRow(
                            entry = b,
                            canRestore = canDestroy,
                            onVerify = { runner.run("Verify ${b.name}", refreshAfter = false) { if (it.backupVerify(b.name)) "${b.name}: OK" else "${b.name}: FAILED integrity check" } },
                            onRestore = { restore = b },
                        )
                    }
                }
            }
        }
    }

    restore?.let { b ->
        ConfirmDialog(
            title = "Restore ${b.name}?",
            body = "Replaces the live world with this snapshot. The server must be stopped; the current " +
                "world is moved aside (never deleted). This cannot be undone in-app.",
            confirmText = "Restore",
            destructive = true,
            typedConfirm = "restore",
            onConfirm = {
                restore = null
                runner.run("Restore ${b.name}", confirmed = true, refreshAfter = false) { c ->
                    val moved = c.backupRestore(b.name); "Restored — previous world kept at ${moved ?: "world.pre-restore"}"
                }
            },
            onDismiss = { restore = null },
        )
    }
}

@Composable
private fun BackupRow(entry: BackupEntry, canRestore: Boolean, onVerify: () -> Unit, onRestore: () -> Unit) {
    McPanel {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(entry.name, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
            if (entry.full) Text("FULL", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.gold)
        }
        Text(
            "${Format.bytes(entry.size)} · ${Format.duration(entry.ageS.toInt())} ago",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 2.dp),
        )
        Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            McButton("Verify", onClick = onVerify, kind = BtnKind.Neutral)
            McButton("Restore", onClick = onRestore, kind = BtnKind.Danger, enabled = canRestore)
        }
        if (!canRestore) {
            Text("Restore needs the destructive capability (enable in Connection).", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 4.dp))
        }
    }
}
