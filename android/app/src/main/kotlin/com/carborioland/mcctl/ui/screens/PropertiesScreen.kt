package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
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
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.Silkscreen
import com.carborioland.mcctl.ui.theme.mc

@Composable
fun PropertiesScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.propsList() }
    val runner = rememberActionRunner(container)
    var editing by remember { mutableStateOf<Pair<String, String>?>(null) }
    val canEdit = Capability.DESTRUCTIVE in container.repository.capabilities

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "server.properties — tap a key to edit. Writes are atomic with a .bak kept on the server" +
                if (canEdit) "." else " (needs the destructive capability).",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(bottom = 8.dp),
        )
        AsyncContent(res.state, onRetry = res.reload) { props ->
            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(1.dp)) {
                items(props.entries.sortedBy { it.key }.toList()) { (k, v) ->
                    PropRow(k, v ?: "", editable = canEdit && k != "rcon.password") { if (canEdit) editing = k to (v ?: "") }
                }
            }
        }
    }

    editing?.let { (key, current) ->
        var value by remember(key) { mutableStateOf(current) }
        AlertDialog(
            onDismissRequest = { editing = null },
            confirmButton = {
                McButton("Save", kind = BtnKind.Primary, onClick = {
                    editing = null
                    runner.run("Set $key", confirmed = true, refreshAfter = false, onComplete = res.reload) {
                        val set = it.propsSet(key, value); "$key = $set"
                    }
                })
            },
            dismissButton = { McButton("Cancel", { editing = null }, kind = BtnKind.Neutral) },
            title = { Text(key, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface) },
            text = {
                Column {
                    Text("The value is validated server-side; an invalid one is rejected.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
                    McTextField("Value", value, { value = it }, modifier = Modifier.padding(top = 10.dp))
                }
            },
            containerColor = MaterialTheme.colorScheme.surface,
        )
    }
}

@Composable
private fun PropRow(key: String, value: String, editable: Boolean, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().let { if (editable) it.clickable(onClick = onClick) else it }.padding(vertical = 8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(key, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f))
        Text(
            value.ifBlank { "—" },
            style = MaterialTheme.typography.bodyMedium.copy(fontFamily = Silkscreen),
            color = MaterialTheme.mc.info,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
            textAlign = androidx.compose.ui.text.style.TextAlign.End,
        )
    }
}
