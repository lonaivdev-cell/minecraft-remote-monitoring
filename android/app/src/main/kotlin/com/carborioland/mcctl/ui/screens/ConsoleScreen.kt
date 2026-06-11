package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.toMutableStateList
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.rpc.RpcException
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun ConsoleScreen(container: AppContainer) {
    val scope = rememberCoroutineScope()
    val lines = remember { mutableListOf<Pair<Boolean, String>>().toMutableStateList() }
    var input by remember { mutableStateOf("") }
    var sending by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()

    fun send() {
        val cmd = input.trim()
        if (cmd.isEmpty() || sending) return
        input = ""
        lines += true to "> $cmd"
        sending = true
        scope.launch {
            try {
                val out = withContext(Dispatchers.IO) { container.repository.requireClient().cmd(cmd) }
                lines += false to (out.ifBlank { "(no output)" })
            } catch (e: RpcException) {
                lines += false to "error: ${e.friendly()}"
            } catch (e: Exception) {
                lines += false to "error: ${e.message}"
            } finally {
                sending = false
            }
        }
    }

    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) listState.animateScrollToItem(lines.size - 1)
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "Sent via RCON over the SSH tunnel (tmux + log fallback).",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.mc.dim,
            modifier = Modifier.padding(bottom = 6.dp),
        )
        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            itemsIndexed(lines) { _, entry ->
                val (sent, text) = entry
                Text(
                    text,
                    style = TerminalTextStyle,
                    color = if (sent) MaterialTheme.mc.grassLight else MaterialTheme.colorScheme.onSurface,
                )
            }
        }
        Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            McTextField(
                label = "Command — e.g. list, say hi, whitelist on",
                value = input,
                onValueChange = { input = it },
                modifier = Modifier.weight(1f),
            )
            McButton(if (sending) "…" else "Send", kind = BtnKind.Primary, enabled = !sending, onClick = { send() },
                modifier = Modifier.padding(start = 10.dp).width(96.dp))
        }
    }
}
