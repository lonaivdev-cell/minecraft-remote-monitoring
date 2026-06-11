package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.CrashReport
import com.carborioland.mcctl.core.model.Postmortem
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.util.Format
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun CrashesScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.crashes(limit = 20) }
    val scope = rememberCoroutineScope()
    var postmortem by remember { mutableStateOf<Postmortem?>(null) }
    var loadingPm by remember { mutableStateOf(false) }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            SectionLabel("Postmortem")
            Text(
                "Deterministic root-cause read of the newest crash: exception, suspected mod, plus " +
                    "watchdog events — no AI, no API key.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            )
            McButton(if (loadingPm) "Analyzing…" else "Run on latest crash", kind = BtnKind.Primary, enabled = !loadingPm, modifier = Modifier.padding(top = 8.dp), onClick = {
                loadingPm = true
                scope.launch {
                    postmortem = runCatching { withContext(Dispatchers.IO) { container.repository.requireClient().postmortem() } }.getOrNull()
                    loadingPm = false
                }
            })
            postmortem?.let { pm -> PostmortemView(pm) }
        }

        Text("Crash reports on the server", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight)
        AsyncContent(res.state, onRetry = res.reload) { crashes ->
            if (crashes.isEmpty()) {
                EmptyHint("No crash reports — that's the good ending.")
            } else {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    items(crashes) { CrashRow(it) }
                }
            }
        }
    }
}

@Composable
private fun PostmortemView(pm: Postmortem) {
    Column(Modifier.padding(top = 10.dp)) {
        if (pm.summary.isEmpty() && pm.crashError != null) {
            Text(pm.crashError!!, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.warning)
        }
        pm.summary.forEach { Text("• $it", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.padding(vertical = 2.dp)) }
        if (pm.nextSteps.isNotEmpty()) {
            Text("Next steps", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.gold, modifier = Modifier.padding(top = 8.dp))
            pm.nextSteps.forEach { Text("→ $it", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim, modifier = Modifier.padding(vertical = 2.dp)) }
        }
        if (pm.summary.isEmpty() && pm.crashError == null) {
            Text("No crash to analyze.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim)
        }
    }
}

@Composable
private fun CrashRow(c: CrashReport) {
    val fmt = remember { SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US) }
    McPanel {
        Text(c.name, style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(
            "${Format.bytes(c.size)} · ${fmt.format(Date(c.mtime * 1000))}",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 2.dp),
        )
    }
}
