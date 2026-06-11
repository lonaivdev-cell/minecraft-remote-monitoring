package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.background
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.ModInfo
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.util.Format

@Composable
fun ModsScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.modsList() }
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(Modifier.fillMaxWidth().padding(bottom = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Metadata read from inside each jar", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            McButton("Rescan", onClick = res.reload, kind = BtnKind.Neutral)
        }
        AsyncContent(res.state, onRetry = res.reload) { mods ->
            if (mods.isEmpty()) {
                EmptyHint("No mods found in the server's mods/ directory.")
            } else {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    item {
                        Text("${mods.size} mods", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight, modifier = Modifier.padding(bottom = 2.dp))
                    }
                    items(mods.sortedBy { it.title.lowercase() }) { ModRow(it) }
                }
            }
        }
    }
}

@Composable
private fun ModRow(m: ModInfo) {
    McPanel {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(m.title, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
            if (m.version.isNotBlank()) {
                Text(m.version, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.info)
            }
        }
        Row(Modifier.fillMaxWidth().padding(top = 2.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            if (m.loader.isNotBlank()) Tag(m.loader, MaterialTheme.mc.grassDark)
            Text(m.file, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
            Text(Format.bytes(m.size), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
        }
        if (m.description.isNotBlank()) {
            Text(m.description, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, maxLines = 3, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 4.dp))
        }
    }
}

@Composable
private fun Tag(text: String, color: androidx.compose.ui.graphics.Color) {
    Box(Modifier.background(color.copy(alpha = 0.45f)).padding(horizontal = 6.dp, vertical = 1.dp)) {
        Text(text.uppercase(), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface)
    }
}
