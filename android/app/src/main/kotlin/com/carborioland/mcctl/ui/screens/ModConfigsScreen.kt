package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.ConfigFile
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.util.Format

/**
 * Browse and edit the files under the server's `config/` directory, grouped by the
 * mod that owns them. Tapping a file opens an editor; saving writes it back (atomic,
 * with a `.bak`) and — on a live server — runs `/reload`. NeoForge live-reloads mods
 * that support it; startup/cached values need the Restart button. Mirrors the desktop
 * GUI's "Mod Configs" page.
 */
@Composable
fun ModConfigsScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.configTree() }
    val canEdit = Capability.DESTRUCTIVE in container.repository.capabilities
    var query by remember { mutableStateOf("") }
    var editing by remember { mutableStateOf<ConfigFile?>(null) }

    editing?.let { file ->
        ConfigEditor(container, file, canEdit) { editing = null }
        return
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "Files under config/, grouped by mod. " +
                if (canEdit) "Tap one to edit — saves live-reload where the mod supports it."
                else "Read-only (needs the destructive capability to save).",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            modifier = Modifier.padding(bottom = 8.dp),
        )
        McTextField("Search config files", query, { query = it }, modifier = Modifier.padding(bottom = 8.dp))
        AsyncContent(res.state, onRetry = res.reload) { files ->
            val filtered = files.filter {
                query.isBlank() || it.path.contains(query, true) || it.group.contains(query, true)
            }
            if (filtered.isEmpty()) {
                EmptyHint("No config files match.")
            } else {
                val groups = filtered.groupBy { it.group }.toSortedMap()
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    item {
                        Text(
                            "${filtered.size} files · ${groups.size} mods",
                            style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight,
                        )
                    }
                    groups.forEach { (group, groupFiles) ->
                        item(key = "header:$group") { SectionLabel("$group (${groupFiles.size})") }
                        items(groupFiles, key = { it.path }) { f -> ConfigFileRow(f) { editing = f } }
                    }
                }
            }
        }
    }
}

@Composable
private fun ConfigFileRow(f: ConfigFile, onClick: () -> Unit) {
    McPanel(Modifier.clickable(onClick = onClick)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                f.name, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
            )
            Text(
                "${f.fmt.ifBlank { "?" }} · ${Format.bytes(f.size)}",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.info,
            )
        }
        if (f.path.contains('/')) {
            Text(
                f.path, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 2.dp),
            )
        }
    }
}

@Composable
private fun ConfigEditor(
    container: AppContainer,
    file: ConfigFile,
    canEdit: Boolean,
    onClose: () -> Unit,
) {
    val res = rememberRpcResource(container, key = file.path) { it.configGet(file.path) }
    val runner = rememberActionRunner(container)
    var text by remember(file.path) { mutableStateOf<String?>(null) }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(
            Modifier.fillMaxWidth().padding(bottom = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            McButton("Back", onClose, kind = BtnKind.Neutral)
            Text(
                file.name, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
            )
            if (canEdit) {
                McButton("Save", kind = BtnKind.Primary, enabled = text != null && !runner.busy, onClick = {
                    text?.let { t ->
                        runner.run("Save ${file.name}", confirmed = true, refreshAfter = false) {
                            it.configSet(file.path, t, reload = true)
                        }
                    }
                })
            }
        }
        Text(
            "Saving writes the file (atomic, .bak kept) and runs /reload on a live server. " +
                "Startup and cached values only fully apply after a restart.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            modifier = Modifier.padding(bottom = 8.dp),
        )
        Box(Modifier.weight(1f).fillMaxWidth()) {
            AsyncContent(res.state, onRetry = res.reload) { content ->
                LaunchedEffect(content.path) { if (text == null) text = content.text }
                McTextField(
                    label = "${content.fmt.ifBlank { "text" }} · ${Format.bytes(content.bytes)}",
                    value = text ?: content.text,
                    onValueChange = { text = it },
                    singleLine = false,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        if (canEdit) {
            McButton(
                "Restart server (full apply)", kind = BtnKind.Danger, enabled = !runner.busy,
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                onClick = { runner.run("Restart server", refreshAfter = true) { it.restart(); "Restarting…" } },
            )
        }
    }
}
