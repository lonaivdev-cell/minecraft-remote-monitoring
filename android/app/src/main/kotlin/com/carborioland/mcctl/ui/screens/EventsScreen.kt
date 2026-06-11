package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.background
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.toMutableStateList
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.WatchdogEvent
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun EventsScreen(container: AppContainer) {
    val repo = container.repository
    val events = remember { mutableListOf<WatchdogEvent>().toMutableStateList() }
    val scope = rememberCoroutineScope()

    // Load history, then ask the agent to stream new events (deduping on ts).
    LaunchedEffect(Unit) {
        runCatching {
            val history = withContext(Dispatchers.IO) { repo.requireClient().eventsList(limit = 100) }
            events.clear()
            events.addAll(history.sortedByDescending { it.ts })
            repo.requireClient().eventsSubscribe()
        }
    }
    DisposableEffect(Unit) {
        val job = scope.launch {
            repo.events.collect { ev ->
                if (events.none { it.ts == ev.ts && it.kind == ev.kind }) events.add(0, ev)
            }
        }
        onDispose {
            job.cancel()
            scope.launch { runCatching { repo.requireClient().eventsUnsubscribe() } }
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "The watchdog's audit log — every heal, restart and alert. Streaming live.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(bottom = 8.dp),
        )
        if (events.isEmpty()) {
            EmptyHint("No events yet. Crashes, restarts and TPS/heap alerts will appear here.")
        } else {
            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(events) { EventRow(it) }
            }
        }
    }
}

@Composable
private fun EventRow(ev: WatchdogEvent) {
    val fmt = remember { SimpleDateFormat("MMM d · HH:mm:ss", Locale.US) }
    val color = if (ev.critical) MaterialTheme.mc.danger else kindColor(ev.kind)
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Box(Modifier.padding(end = 10.dp, top = 2.dp).background(color).size(width = 4.dp, height = 36.dp))
        Column(Modifier.weight(1f)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(ev.kind.uppercase(), style = MaterialTheme.typography.labelMedium, color = color)
                Text(fmt.format(Date((ev.ts * 1000).toLong())), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
            }
            if (ev.detail.isNotBlank()) {
                Text(ev.detail, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface)
            }
        }
    }
}

@Composable
private fun kindColor(kind: String): Color = when {
    kind.startsWith("alert") -> MaterialTheme.mc.warning
    kind.contains("crash") || kind.contains("halt") -> MaterialTheme.mc.danger
    kind == "started" -> MaterialTheme.mc.success
    kind == "stopped" -> MaterialTheme.mc.dim
    else -> MaterialTheme.mc.info
}
