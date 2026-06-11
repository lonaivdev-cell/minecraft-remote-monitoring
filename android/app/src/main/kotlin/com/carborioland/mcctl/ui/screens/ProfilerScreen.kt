package com.carborioland.mcctl.ui.screens

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc

@Composable
fun ProfilerScreen(container: AppContainer) {
    val runner = rememberActionRunner(container)
    var seconds by remember { mutableFloatStateOf(60f) }
    var url by remember { mutableStateOf<String?>(null) }
    val uriHandler = LocalUriHandler.current

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            SectionLabel("spark profiler")
            Text(
                "Runs spark's async profiler on the live server, then returns a shareable viewer URL " +
                    "at spark.lucko.me — open it to see exactly where ticks are spent.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
            )
            Text("Duration: ${seconds.toInt()}s", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.padding(top = 10.dp))
            Slider(
                value = seconds,
                onValueChange = { seconds = it },
                valueRange = 15f..180f,
                steps = 10,
                colors = SliderDefaults.colors(thumbColor = MaterialTheme.mc.grassLight, activeTrackColor = MaterialTheme.mc.grassFill),
            )
            McButton(if (runner.busy) "Profiling…" else "Run profiler", kind = BtnKind.Primary, enabled = !runner.busy, modifier = Modifier.fillMaxWidth(), onClick = {
                runner.run("Run spark profiler (${seconds.toInt()}s)", refreshAfter = false) { c ->
                    url = c.profile(seconds.toInt())
                    "Profile ready"
                }
            })
        }

        url?.let { u ->
            McPanel {
                SectionLabel("Result")
                Text(u, style = TerminalTextStyle, color = MaterialTheme.mc.info, modifier = Modifier.padding(vertical = 6.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    McButton("Open viewer", kind = BtnKind.Primary, onClick = { runCatching { uriHandler.openUri(u) } })
                }
            }
        }
    }
}
