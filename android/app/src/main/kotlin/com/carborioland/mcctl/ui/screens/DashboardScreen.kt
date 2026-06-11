package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.ServerState
import com.carborioland.mcctl.core.model.Status
import com.carborioland.mcctl.data.ConnState
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.ConfirmDialog
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McSwitchRow
import com.carborioland.mcctl.ui.components.PixelGauge
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.components.StatusBadge
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.theme.mc
import com.carborioland.mcctl.util.Format
import kotlinx.coroutines.delay

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DashboardScreen(container: AppContainer, onNeedConnection: () -> Unit) {
    val repo = container.repository
    val connState by repo.state.collectAsStateWithLifecycle()
    val status by repo.status.collectAsStateWithLifecycle()
    val runner = rememberActionRunner(container)
    var booting by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf<ConfirmSpec?>(null) }

    if (connState !is ConnState.Connected) {
        DisconnectedPrompt(connState, onNeedConnection)
        return
    }

    // Poll while the dashboard is on screen: cheap probe every 5s, full every 20s — the
    // same cadence as the desktop GUI's FAST_TICK / SLOW_TICK.
    LaunchedEffect(Unit) {
        var n = 0
        while (true) {
            runCatching { repo.refresh(fast = n % 4 != 0) }
            n++
            delay(5_000)
        }
    }
    LaunchedEffect(status?.baseState()) {
        if (status?.baseState() == ServerState.ONLINE) booting = false
    }

    val st = status
    val state = if (booting && st?.baseState() != ServerState.ONLINE) ServerState.BOOTING else (st?.baseState() ?: ServerState.CONNECTING)
    val canAct = Capability.ACTIONS in repo.capabilities

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        StatusBadge(state.badgeText(), state.badgeColor())
        Text(
            container.profileLabel(),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.mc.dim,
            textAlign = TextAlign.Center,
        )

        ActionGrid(
            state = state,
            canAct = canAct,
            busy = runner.busy,
            onStart = {
                booting = true
                runner.run("Start the server", onComplete = { booting = false }) { it.start(); "Server is up" }
            },
            onStop = { confirm = ConfirmSpec.stop(st) },
            onRestart = { confirm = ConfirmSpec.restart() },
            onSave = { runner.run("Save the world") { if (it.save(skipIfDown = true)) "World saved" else "save-all sent" } },
            onBackup = { runner.run("Back up now", refreshAfter = false) { c -> c.backupCreate()?.let { "Backup: ${it.name}" } ?: "Backup created" } },
            onPurge = { runner.run("Purge GC") { "Purge: ${it.purge()["verdict"] ?: "done"}" } },
        )

        ServerCard(st)
        HostCard(st)
        WatchdogCard(st, container, canAct)
    }

    confirm?.let { spec ->
        ConfirmDialog(
            title = spec.title,
            body = spec.body,
            confirmText = spec.confirmText,
            destructive = spec.destructive,
            onConfirm = {
                val s = spec
                confirm = null
                when (s.kind) {
                    ActionKind.STOP -> runner.run("Stop", confirmed = true) { it.stop(); "Server stopped" }
                    ActionKind.RESTART -> {
                        booting = true
                        runner.run("Restart", confirmed = true, onComplete = { booting = false }) { it.restart(); "Server restarted" }
                    }
                }
            },
            onDismiss = { confirm = null },
        )
    }
}

private enum class ActionKind { STOP, RESTART }

private data class ConfirmSpec(
    val kind: ActionKind,
    val title: String,
    val body: String,
    val confirmText: String,
    val destructive: Boolean,
) {
    companion object {
        fun stop(st: Status?): ConfirmSpec {
            val n = st?.players?.count ?: 0
            val body = if (n > 0) {
                "$n player(s) online — they get a countdown, the world is flushed, then the server stops. " +
                    "The watchdog stands down (desired=down)."
            } else {
                "The world is flushed to disk, then the server stops. The watchdog stands down."
            }
            return ConfirmSpec(ActionKind.STOP, "Stop the server?", body, "Stop", destructive = true)
        }

        fun restart() = ConfirmSpec(
            ActionKind.RESTART, "Restart the server?",
            "Graceful stop (countdown + save) followed by a fresh boot.", "Restart", destructive = false,
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ActionGrid(
    state: ServerState,
    canAct: Boolean,
    busy: Boolean,
    onStart: () -> Unit,
    onStop: () -> Unit,
    onRestart: () -> Unit,
    onSave: () -> Unit,
    onBackup: () -> Unit,
    onPurge: () -> Unit,
) {
    val running = state == ServerState.ONLINE || state == ServerState.BOOTING
    val idle = !busy && canAct
    FlowRow(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        McButton("Start", onStart, kind = BtnKind.Primary, enabled = idle && !running)
        McButton("Stop", onStop, kind = BtnKind.Danger, enabled = idle && running)
        McButton("Restart", onRestart, enabled = idle && running)
        McButton("Save", onSave, enabled = idle && running)
        McButton("Back up", onBackup, enabled = idle)
        McButton("Purge GC", onPurge, enabled = idle && running)
    }
    if (!canAct) {
        Text(
            "Read-only session — enable 'actions' in Connection to control the server.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.mc.warning,
        )
    }
}

@Composable
private fun ServerCard(st: Status?) {
    McPanel {
        SectionLabel("Server")
        if (st == null) {
            KeyValue("Process", "—")
            return@McPanel
        }
        KeyValue(
            "Process",
            if (st.running) buildString {
                append("pid ${st.pid} · up ${Format.duration(st.uptimeS)}")
                st.tmuxSession?.let { append(" · tmux '$it'") }
                if (st.paneDead) append(" · DEAD PANE!")
            } else "not running",
            valueColor = if (st.paneDead) MaterialTheme.mc.danger else null,
        )
        val players = st.players
        KeyValue(
            "Players",
            if (players != null) "${players.count}/${players.max}${if (players.names.isNotEmpty()) " — ${players.names.joinToString(", ")}" else ""}" else "—",
        )
        val tps = st.tpsNow()
        KeyValue("TPS", tps?.let { "%.1f%s".format(it, st.msptMedian()?.let { m -> " · %.1f ms".format(m) } ?: "") } ?: "—",
            valueColor = tps?.let { tpsColor(it) })
        if (tps != null) PixelGauge(fraction = (tps / 20f).toFloat(), tint = tpsColor(tps), modifier = Modifier.padding(top = 2.dp, bottom = 6.dp))
        HeapRow(st)
        KeyValue("Console channel", st.channel ?: "—")
    }
}

@Composable
private fun HeapRow(st: Status) {
    val used = st.heapUsed
    val max = st.heapMax ?: st.heapCommitted
    KeyValue("Heap", if (used != null && max != null) "${Format.bytes(used)} / ${Format.bytes(max)}" else "—")
    if (used != null && max != null && max > 0) {
        val frac = (used.toFloat() / max)
        PixelGauge(frac, tint = if (frac > 0.92f) MaterialTheme.mc.danger else MaterialTheme.mc.info, modifier = Modifier.padding(top = 2.dp, bottom = 4.dp))
    }
}

@Composable
private fun HostCard(st: Status?) {
    McPanel {
        SectionLabel("Host")
        val memUsed = st?.hostMemUsed
        val memTotal = st?.hostMemTotal
        KeyValue("RAM", if (memUsed != null && memTotal != null) "${Format.bytes(memUsed)} / ${Format.bytes(memTotal)}" else "—")
        if (memUsed != null && memTotal != null && memTotal > 0) {
            PixelGauge(memUsed.toFloat() / memTotal, tint = MaterialTheme.mc.info, modifier = Modifier.padding(top = 2.dp, bottom = 4.dp))
        }
        KeyValue("Load", st?.load?.joinToString(" ") { "%.2f".format(it) } ?: "—")
        KeyValue("Disk free", Format.bytes(st?.diskFree))
        KeyValue("Log activity", st?.logAgeS?.let { "last write ${Format.duration(it)} ago" } ?: "—")
        KeyValue("Last backup", st?.lastBackup?.let { "$it · ${Format.duration(st.lastBackupAgeS)} ago" } ?: "none yet")
    }
}

@Composable
private fun WatchdogCard(st: Status?, container: AppContainer, canAct: Boolean) {
    val runner = rememberActionRunner(container)
    McPanel {
        SectionLabel("Watchdog")
        McSwitchRow(
            "Armed",
            checked = st?.armed == true,
            onCheckedChange = { armed ->
                runner.run(if (armed) "Arm the watchdog" else "Disarm the watchdog") {
                    if (armed) it.watchdogArm() else it.watchdogDisarm()
                    if (armed) "Watchdog armed" else "Watchdog disarmed"
                }
            },
            subtitle = "Heal crashes & freezes automatically while desired=up",
        )
        KeyValue(
            "State",
            "desired=${st?.desired ?: "?"}${if (st?.halted == true) " · HALTED (crash-loop breaker)" else ""}",
            valueColor = if (st?.halted == true) MaterialTheme.mc.danger else null,
        )
        if (!canAct) {
            Text("Read-only — arm/disarm needs the actions capability.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
        }
    }
}

@Composable
private fun DisconnectedPrompt(state: ConnState, onConnect: () -> Unit) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        McPanel {
            SectionLabel("Not connected")
            Text(
                when (state) {
                    is ConnState.Failed -> "Connection failed: ${state.message}"
                    ConnState.Connecting -> "Connecting…"
                    else -> "Open a session to your CarborioLand box to see live status and controls."
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.mc.dim,
            )
            McButton("Go to Connection", onConnect, kind = BtnKind.Primary, modifier = Modifier.padding(top = 12.dp).fillMaxWidth())
        }
    }
}

@Composable
private fun ServerState.badgeColor() = when (this) {
    ServerState.ONLINE -> MaterialTheme.mc.online
    ServerState.BOOTING -> MaterialTheme.mc.booting
    ServerState.OFFLINE -> MaterialTheme.mc.offline
    ServerState.UNREACHABLE -> MaterialTheme.mc.unreachable
    ServerState.CONNECTING -> MaterialTheme.mc.dim
}

private fun ServerState.badgeText() = when (this) {
    ServerState.ONLINE -> "Online"
    ServerState.BOOTING -> "Booting"
    ServerState.OFFLINE -> "Offline"
    ServerState.UNREACHABLE -> "Unreachable"
    ServerState.CONNECTING -> "Connecting"
}

@Composable
private fun tpsColor(tps: Double) = when {
    tps >= 18.0 -> MaterialTheme.mc.success
    tps >= 12.0 -> MaterialTheme.mc.warning
    else -> MaterialTheme.mc.danger
}

private fun AppContainer.profileLabel(): String = "tap ⟳ to refresh · live status"
