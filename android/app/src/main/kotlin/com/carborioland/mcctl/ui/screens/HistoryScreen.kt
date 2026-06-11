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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.MetricSample
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.MetricChartCard
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc

private data class HistorySpec(val key: String, val label: String, val fixedMax: Float?, val format: (Float) -> String, val color: Color)

@Composable
fun HistoryScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.metricsHistory(n = 180) }

    val specs = listOf(
        HistorySpec("tps", "TPS", 20f, { "%.1f".format(it) }, Color(0xFF38BD73)),
        HistorySpec("mspt", "MSPT", null, { "%.0f ms".format(it) }, Color(0xFFEE9C38)),
        HistorySpec("heap", "Heap", 100f, { "%.0f%%".format(it) }, Color(0xFF549AF2)),
        HistorySpec("players", "Players", null, { "%.0f".format(it) }, Color(0xFFA876EB)),
        HistorySpec("mem", "Host RAM", 100f, { "%.0f%%".format(it) }, Color(0xFF3DB8B8)),
        HistorySpec("load", "Load 1m", null, { "%.2f".format(it) }, Color(0xFFE87373)),
    )

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Recorded by the watchdog, mcctl watch & dash", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            McButton("Reload", onClick = res.reload, kind = BtnKind.Neutral)
        }
        AsyncContent(res.state, onRetry = res.reload) { samples ->
            if (samples.isEmpty()) {
                EmptyHint("No metric history recorded yet. It fills in as the watchdog and dash run.")
            } else {
                specs.forEach { spec ->
                    val values: List<Float?> = samples.map { s: MetricSample -> s.value(spec.key)?.toFloat() }
                    MetricChartCard(
                        title = spec.label,
                        values = values,
                        color = spec.color,
                        fixedMax = spec.fixedMax,
                        format = spec.format,
                    )
                }
            }
        }
    }
}
