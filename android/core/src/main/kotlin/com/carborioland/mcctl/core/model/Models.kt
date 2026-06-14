package com.carborioland.mcctl.core.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Typed mirrors of the `mcctl agent` JSON-RPC contract (golden schema, protocol 1).
 *
 * Field names use camelCase; the [com.carborioland.mcctl.core.rpc.McctlJson] instance
 * maps them to the wire's snake_case, so `uptimeS` <-> `uptime_s`. Every payload field
 * is optional with a safe default — the contract is additive within a major version, so
 * an older app must tolerate new fields (handled by `ignoreUnknownKeys`) and a newer app
 * must tolerate missing ones (handled by defaults).
 */

/** Derived, UI-facing lifecycle state — mirrors the desktop GUI's status badge. */
enum class ServerState { ONLINE, BOOTING, OFFLINE, UNREACHABLE, CONNECTING }

@Serializable
data class PlayerList(
    val count: Int = 0,
    val max: Int = 0,
    val names: List<String> = emptyList(),
)

@Serializable
data class Status(
    val running: Boolean = false,
    val pid: Int? = null,
    val uptimeS: Int? = null,
    val tmux: Boolean = false,
    val tmuxSession: String? = null,
    val paneDead: Boolean = false,
    val portOpen: Boolean = false,
    val logAgeS: Int? = null,
    val heapUsed: Long? = null,
    val heapCommitted: Long? = null,
    val heapMax: Long? = null,
    val hostMemTotal: Long? = null,
    val hostMemUsed: Long? = null,
    val hostMemAvail: Long? = null,
    val load: List<Double>? = null,
    val diskFree: Long? = null,
    val players: PlayerList? = null,
    val tps: JsonObject? = null,
    val channel: String? = null,
    val desired: String = "down",
    val armed: Boolean = false,
    val halted: Boolean = false,
    val lastBackup: String? = null,
    val lastBackupAgeS: Int? = null,
    val errors: List<String> = emptyList(),
) {
    /** Most recent TPS reading, preferring the shortest window — matches the GUI. */
    fun tpsNow(): Double? {
        val t = tps?.get("tps")?.let { (it as? JsonObject) } ?: return null
        for (k in listOf("10s", "5s", "1m")) t[k]?.jsonPrimitive?.doubleOrNull?.let { return it }
        return t.values.firstOrNull()?.jsonPrimitive?.doubleOrNull
    }

    fun msptMedian(): Double? =
        (tps?.get("mspt") as? JsonObject)?.get("median")?.jsonPrimitive?.doubleOrNull

    /** The base lifecycle state, before any client-side "booting during a start action". */
    fun baseState(): ServerState = when {
        errors.isNotEmpty() -> ServerState.UNREACHABLE
        running && portOpen -> ServerState.ONLINE
        running -> ServerState.BOOTING
        else -> ServerState.OFFLINE
    }
}

@Serializable
data class BackupEntry(
    val name: String = "",
    val path: String = "",
    val ts: String = "",
    val size: Long = 0,
    val full: Boolean = false,
    val ageS: Double = 0.0,
)

@Serializable
data class WatchdogEvent(
    val ts: Double = 0.0,
    val kind: String = "",
    val detail: String = "",
    val urgency: String = "normal",
    val data: JsonObject? = null,
) {
    val critical: Boolean get() = urgency == "critical"
}

@Serializable
data class ModInfo(
    val file: String = "",
    val size: Long = 0,
    val mtime: Long = 0,
    val modId: String = "",
    val name: String = "",
    val version: String = "",
    val loader: String = "",
    val description: String = "",
) {
    val title: String get() = name.ifBlank { file }
}

/** One file under `config/` as listed by `config.tree`. */
@Serializable
data class ConfigFile(
    val path: String = "",
    val size: Long = 0,
    val mtime: Long = 0,
    val fmt: String = "",
    val modId: String = "",
    val modName: String = "",
) {
    /** Just the filename, for a compact row title. */
    val name: String get() = path.substringAfterLast('/')

    /** Picker group: the owning mod, or a catch-all for the unassociated files. */
    val group: String get() = modName.ifBlank { modId }.ifBlank { "Other / unmatched" }
}

/** The contents of one config file, from `config.get`. */
@Serializable
data class ConfigContent(
    val path: String = "",
    val text: String = "",
    val fmt: String = "",
    val bytes: Long = 0,
)

/**
 * A flattened metrics sample as written by `metrics.sample_from_status`. The history
 * charts derive percentages from the raw byte counts exactly like the desktop GUI.
 */
@Serializable
data class MetricSample(
    val ts: Long = 0,
    val running: Boolean = false,
    val players: Int? = null,
    val tps: Double? = null,
    val mspt: Double? = null,
    val heapUsed: Long? = null,
    val heapCommitted: Long? = null,
    val heapMax: Long? = null,
    val memUsed: Long? = null,
    val memTotal: Long? = null,
    val load1: Double? = null,
    val diskFree: Long? = null,
    val logAge: Int? = null,
) {
    /** Value for a History card key — mirrors `gui_app._history_value`. */
    fun value(key: String): Double? = when (key) {
        "tps" -> tps
        "mspt" -> mspt
        "players" -> players?.toDouble()
        "load" -> load1
        "heap" -> percent(heapUsed, heapMax ?: heapCommitted)
        "mem" -> percent(memUsed, memTotal)
        else -> null
    }

    private fun percent(used: Long?, total: Long?): Double? =
        if (used != null && total != null && total > 0) 100.0 * used / total else null
}

@Serializable
data class InspectorSection(
    val section: String = "",
    val title: String = "",
    val data: JsonObject? = null,
)

@Serializable
data class Postmortem(
    val crash: JsonObject? = null,
    val crashError: String? = null,
    val summary: List<String> = emptyList(),
    val nextSteps: List<String> = emptyList(),
    val events: List<JsonObject> = emptyList(),
    val evidence: List<JsonObject> = emptyList(),
)

@Serializable
data class CrashReport(
    val name: String = "",
    val size: Long = 0,
    val mtime: Long = 0,
)

@Serializable
data class JvmInfo(
    val java: String? = null,
    val xms: String? = null,
    val xmx: String? = null,
    val javaArgs: String? = null,
)

@Serializable
data class WatchdogState(
    val armed: Boolean = false,
    val desired: String = "down",
    val halted: Boolean = false,
    val restarts: List<Double> = emptyList(),
)

@Serializable
data class HelloResult(
    val protocol: Int = 0,
    val mcctlVersion: String = "",
    val capabilities: List<String> = emptyList(),
    val methods: List<String> = emptyList(),
)

/** spark TPS reading. Windows/stats are free-form maps; helpers pull the common ones. */
@Serializable
data class TpsReport(
    val tps: Map<String, Double> = emptyMap(),
    val mspt: Map<String, Double> = emptyMap(),
    val cpuSystem: Map<String, Double> = emptyMap(),
    val cpuProcess: Map<String, Double> = emptyMap(),
) {
    fun tpsNow(): Double? {
        for (k in listOf("10s", "5s", "1m")) tps[k]?.let { return it }
        return tps.values.firstOrNull()
    }

    fun msptMedian(): Double? = mspt["median"]
}

@Serializable
data class HealthReport(
    val tps: Map<String, Double> = emptyMap(),
    val memoryUsed: Long? = null,
    val memoryMax: Long? = null,
    val diskUsed: Long? = null,
    val diskTotal: Long? = null,
)

/** Capabilities the client may request in `agent.hello`. */
enum class Capability(val wire: String) {
    /** Lifecycle + console + player + backup actions. */
    ACTIONS("actions"),

    /** State-destroying methods (kill, restore, props.set, jvm.heap, players.ban). */
    DESTRUCTIVE("destructive"),
}

/** Helper for inspector sections: the canonical order, mirroring `inspector.SECTIONS`. */
val INSPECTOR_SECTIONS = listOf(
    "host", "tree", "proc", "threads", "memory", "fds", "net", "env",
    "jvm", "systemd", "init", "sshd", "bash", "sh", "tmux", "java",
)

/** Extract a plain string from a JSON object key, or null. */
fun JsonObject.str(key: String): String? = this[key]?.jsonPrimitive?.contentOrNull

/** Render a free-form inspector `data` object as flat "key: value" lines for display. */
fun InspectorSection.lines(): List<Pair<String, String>> {
    val d = data ?: return emptyList()
    return d.entries.map { (k, v) ->
        val text = when (v) {
            is JsonObject -> v.jsonObject.entries.joinToString(", ") { "${it.key}=${it.value}" }
            else -> v.toString().removeSurrounding("\"")
        }
        k to text
    }
}
