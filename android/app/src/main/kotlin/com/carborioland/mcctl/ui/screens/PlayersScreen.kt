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
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.ActionRunner
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.ConfirmDialog
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc

@Composable
fun PlayersScreen(container: AppContainer) {
    val res = rememberRpcResource(container) { it.playersList() }
    val runner = rememberActionRunner(container)
    var add by remember { mutableStateOf("") }
    var banTarget by remember { mutableStateOf<String?>(null) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        McPanel {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                SectionLabel("Online players")
                McButton("Reload", onClick = res.reload, kind = BtnKind.Neutral)
            }
            AsyncContent(res.state, onRetry = res.reload) { pl ->
                val players = pl
                if (players == null || players.names.isEmpty()) {
                    EmptyHint(if (players == null) "Server offline or RCON unavailable." else "Nobody online right now.")
                } else {
                    Text("${players.count}/${players.max} online", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight, modifier = Modifier.padding(vertical = 4.dp))
                    players.names.forEach { name ->
                        PlayerRow(
                            name = name,
                            onOp = { runner.run("Op $name", onComplete = res.reload) { it.op(name); "$name opped" } },
                            onKick = { runner.run("Kick $name", onComplete = res.reload) { it.kick(name); "$name kicked" } },
                            onBan = { banTarget = name },
                        )
                    }
                }
            }
        }

        McPanel {
            SectionLabel("Whitelist")
            Row(Modifier.fillMaxWidth().padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                McTextField("Add player", add, { add = it }, modifier = Modifier.weight(1f))
                McButton("Add", kind = BtnKind.Primary, enabled = add.isNotBlank(), modifier = Modifier.padding(start = 10.dp), onClick = {
                    val n = add.trim(); add = ""
                    runner.run("Whitelist $n") { it.whitelist(n, "add"); "$n whitelisted" }
                })
            }
            Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                McButton("Enforce ON", onClick = { runner.run("Whitelist on") { it.whitelist("", "on"); "Whitelist enforced" } })
                McButton("Enforce OFF", onClick = { runner.run("Whitelist off") { it.whitelist("", "off"); "Whitelist off" } })
            }
        }
    }

    banTarget?.let { name ->
        ConfirmDialog(
            title = "Ban $name?",
            body = "This kicks and bans the player. Needs the destructive capability.",
            confirmText = "Ban",
            destructive = true,
            onConfirm = {
                banTarget = null
                runner.run("Ban $name", confirmed = true, onComplete = res.reload) { it.ban(name); "$name banned" }
            },
            onDismiss = { banTarget = null },
        )
    }
}

@Composable
private fun PlayerRow(name: String, onOp: () -> Unit, onKick: () -> Unit, onBan: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(name, style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            McButton("Op", onClick = onOp, kind = BtnKind.Neutral)
            McButton("Kick", onClick = onKick, kind = BtnKind.Neutral)
            McButton("Ban", onClick = onBan, kind = BtnKind.Danger)
        }
    }
}
