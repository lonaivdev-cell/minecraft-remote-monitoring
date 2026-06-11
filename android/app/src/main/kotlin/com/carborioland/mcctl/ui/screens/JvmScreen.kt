package com.carborioland.mcctl.ui.screens

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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc

@Composable
fun JvmScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.jvmShow() }
    val runner = rememberActionRunner(container)
    var heap by remember { mutableStateOf("") }
    val canEdit = Capability.DESTRUCTIVE in container.repository.capabilities

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        AsyncContent(res.state, onRetry = res.reload) { jvm ->
            McPanel {
                SectionLabel("Effective configuration")
                KeyValue("JAVA", jvm.java ?: "—")
                KeyValue("Xms", jvm.xms ?: "—")
                KeyValue("Xmx", jvm.xmx ?: "—")
                if (!jvm.javaArgs.isNullOrBlank()) {
                    Text("JAVA_ARGS", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 6.dp))
                    Text(jvm.javaArgs!!, style = TerminalTextStyle, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.padding(top = 4.dp))
                }
            }
        }

        McPanel {
            SectionLabel("Set heap")
            Text(
                "Rewrites Xms = Xmx in variables.txt (Aikar's flags preserved). Applies on next restart." +
                    if (canEdit) "" else " Needs the destructive capability.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            )
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                McTextField("Heap, e.g. 12G", heap, { heap = it }, modifier = Modifier.weight(1f))
                McButton("Apply", kind = BtnKind.Primary, enabled = canEdit && heap.isNotBlank(), modifier = Modifier.padding(start = 10.dp), onClick = {
                    val size = heap.trim(); heap = ""
                    runner.run("Set heap to $size", confirmed = true, refreshAfter = false, onComplete = res.reload) {
                        it.jvmHeap(size); "Heap set to $size (restart to apply)"
                    }
                })
            }
        }
    }
}
