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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc

@Composable
fun LogsScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.logsTail(lines = 250) }
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(Modifier.fillMaxWidth().padding(bottom = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Text("latest.log — newest first", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            McButton("Reload", onClick = res.reload, kind = BtnKind.Neutral)
        }
        AsyncContent(res.state, onRetry = res.reload) { lines ->
            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(1.dp)) {
                items(lines.reversed()) { line ->
                    Text(line, style = TerminalTextStyle, color = lineColor(line))
                }
            }
        }
    }
}

@Composable
private fun lineColor(line: String) = when {
    line.contains("ERROR", true) || line.contains("Exception") -> MaterialTheme.mc.danger
    line.contains("WARN", true) -> MaterialTheme.mc.warning
    else -> MaterialTheme.colorScheme.onSurface
}
