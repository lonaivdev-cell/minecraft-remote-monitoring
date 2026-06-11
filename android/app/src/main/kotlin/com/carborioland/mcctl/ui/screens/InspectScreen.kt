package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.INSPECTOR_SECTIONS
import com.carborioland.mcctl.core.model.lines
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.Silkscreen
import com.carborioland.mcctl.ui.theme.mc

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InspectScreen(container: AppContainer) {
    var section by remember { mutableStateOf(INSPECTOR_SECTIONS.first()) }
    var expanded by remember { mutableStateOf(false) }
    val res = rememberRpcResource(container, key = section) { it.inspect(section) }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "Live kernel/JVM state: /proc, threads, memory maps, fds, sockets, jcmd.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(bottom = 8.dp),
        )
        ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
            OutlinedTextField(
                value = section,
                onValueChange = {},
                readOnly = true,
                label = { Text("Section") },
                textStyle = MaterialTheme.typography.bodyLarge.copy(fontFamily = Silkscreen),
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = MaterialTheme.mc.grassFill,
                    unfocusedBorderColor = MaterialTheme.colorScheme.outline,
                ),
                modifier = Modifier.menuAnchor().fillMaxWidth(),
            )
            ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                INSPECTOR_SECTIONS.forEach { s ->
                    DropdownMenuItem(text = { Text(s) }, onClick = { section = s; expanded = false })
                }
            }
        }

        Column(Modifier.fillMaxSize().padding(top = 12.dp)) {
            AsyncContent(res.state, onRetry = res.reload) { sec ->
                McPanel {
                    Text(sec.title.ifBlank { sec.section }, style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold), color = MaterialTheme.mc.grassLight, modifier = Modifier.padding(bottom = 6.dp))
                    val rows = sec.lines()
                    if (rows.isEmpty()) {
                        Text("(no data)", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim)
                    } else {
                        rows.forEach { (k, v) -> KeyValue(k, v) }
                    }
                }
            }
        }
    }
}
