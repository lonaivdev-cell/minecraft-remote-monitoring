package com.carborioland.mcctl.core.rpc

import com.carborioland.mcctl.core.model.BackupEntry
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.CrashReport
import com.carborioland.mcctl.core.model.HealthReport
import com.carborioland.mcctl.core.model.HelloResult
import com.carborioland.mcctl.core.model.InspectorSection
import com.carborioland.mcctl.core.model.JvmInfo
import com.carborioland.mcctl.core.model.MetricSample
import com.carborioland.mcctl.core.model.ModInfo
import com.carborioland.mcctl.core.model.PlayerList
import com.carborioland.mcctl.core.model.Postmortem
import com.carborioland.mcctl.core.model.Status
import com.carborioland.mcctl.core.model.TpsReport
import com.carborioland.mcctl.core.model.WatchdogEvent
import com.carborioland.mcctl.core.model.WatchdogState
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/**
 * A typed client for the `mcctl agent` JSON-RPC contract. One [AgentClient] drives one
 * [AgentTransport] (one SSH channel). Requests are correlated by id; server-initiated
 * `event` notifications fan out on [events]. Destructive methods send `confirm: true`
 * automatically — the UI is responsible for asking the user *before* calling them.
 *
 * Concurrency mirrors the agent's: requests serialize on the wire, but each `call`
 * suspends independently on its own reply, so the ViewModel layer can fire-and-await
 * without blocking a thread.
 */
class AgentClient(
    private val transport: AgentTransport,
    private val scope: CoroutineScope,
) {
    private val ids = AtomicInteger(0)
    private val pending = ConcurrentHashMap<Int, CompletableDeferred<JsonElement?>>()
    private var readerJob: Job? = null

    private val _events = MutableSharedFlow<WatchdogEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<WatchdogEvent> = _events.asSharedFlow()

    var capabilities: Set<Capability> = emptySet()
        private set
    var hello: HelloResult? = null
        private set

    /** Begin reading the transport. Call once, before the first [callRaw]. */
    fun open() {
        if (readerJob != null) return
        readerJob = scope.launch {
            transport.receiveLines().collect { line ->
                when (val msg = parseInbound(line)) {
                    is Inbound.Reply -> pending.remove(msg.id)?.let { d ->
                        if (msg.error != null) d.completeExceptionally(msg.error)
                        else d.complete(msg.result)
                    }
                    is Inbound.Event ->
                        _events.emit(McctlJson.decodeFromJsonElement(WatchdogEvent.serializer(), msg.params))
                    is Inbound.Unparseable -> Unit // a stray line on the channel; ignore
                }
            }
            // Channel closed: fail anything still waiting so callers don't hang forever.
            pending.values.forEach { it.completeExceptionally(RpcException(RpcCodes.INTERNAL, "agent channel closed")) }
            pending.clear()
        }
    }

    suspend fun shutdownAndClose() {
        runCatching { callRaw("agent.shutdown") }
        readerJob?.cancel()
        transport.close()
    }

    // ------------------------------------------------------------------ raw call

    /** Send a request and await its reply. Throws [RpcException] on an error envelope. */
    suspend fun callRaw(method: String, params: JsonObject? = null): JsonObject? {
        val id = ids.incrementAndGet()
        val deferred = CompletableDeferred<JsonElement?>()
        pending[id] = deferred
        try {
            transport.send(buildRequestLine(id, method, params))
        } catch (e: Throwable) {
            pending.remove(id)
            throw RpcException(RpcCodes.APP, "send failed: ${e.message}", null)
        }
        return when (val r = deferred.await()) {
            null, is JsonNull -> null
            is JsonObject -> r
            else -> null
        }
    }

    private fun <T> decode(obj: JsonObject?, deser: DeserializationStrategy<T>): T =
        McctlJson.decodeFromJsonElement(deser, obj ?: JsonObject(emptyMap()))

    // ------------------------------------------------------------------ handshake

    suspend fun hello(caps: Set<Capability>): HelloResult {
        val params = buildJsonObject {
            put("capabilities", buildJsonArrayOf(caps.map { it.wire }))
        }
        val result = decode(callRaw("agent.hello", params), HelloResult.serializer())
        capabilities = result.capabilities.mapNotNull { wire ->
            Capability.entries.firstOrNull { it.wire == wire }
        }.toSet()
        hello = result
        return result
    }

    suspend fun ping(): Double =
        callRaw("agent.ping")?.get("pong")?.jsonPrimitive?.double ?: 0.0

    // ------------------------------------------------------------------ status

    suspend fun status(fast: Boolean = false): Status =
        decode(callRaw("status", obj { put("fast", JsonPrimitive(fast)) }), Status.serializer())

    // ------------------------------------------------------------------ lifecycle

    suspend fun start() = ackOf(callRaw("start"))
    suspend fun stop(now: Boolean = false, reason: String = "") =
        ackOf(callRaw("stop", obj { put("now", JsonPrimitive(now)); put("reason", JsonPrimitive(reason)) }))

    suspend fun restart(now: Boolean = false, reason: String = "restart") =
        ackOf(callRaw("restart", obj { put("now", JsonPrimitive(now)); put("reason", JsonPrimitive(reason)) }))

    /** Emergency stop — destructive, sends confirm:true. Confirm with the user first. */
    suspend fun kill() = ackOf(callRaw("kill", confirmed()))

    suspend fun save(skipIfDown: Boolean = false): Boolean =
        callRaw("save", obj { put("skip_if_down", JsonPrimitive(skipIfDown)) })
            ?.get("saved")?.jsonPrimitive?.booleanOrNull ?: false

    // ------------------------------------------------------------------ console

    suspend fun cmd(command: String): String =
        callRaw("cmd", obj { put("command", JsonPrimitive(command)) })?.string("output") ?: ""

    // ------------------------------------------------------------------ spark

    suspend fun tps(): TpsReport = decode(callRaw("tps"), TpsReport.serializer())
    suspend fun health(): HealthReport = decode(callRaw("health"), HealthReport.serializer())
    suspend fun profile(seconds: Int = 60): String =
        callRaw("profile", obj { put("seconds", JsonPrimitive(seconds)) })?.string("url") ?: ""

    suspend fun purge(): JsonObject = callRaw("purge") ?: JsonObject(emptyMap())

    // ------------------------------------------------------------------ players

    suspend fun playersList(): PlayerList? =
        callRaw("players.list")?.let { decode(it, PlayerList.serializer()) }

    suspend fun whitelist(name: String, action: String) =
        outputOf(callRaw("players.whitelist", obj {
            put("name", JsonPrimitive(name)); put("action", JsonPrimitive(action))
        }))

    suspend fun op(name: String, deop: Boolean = false) =
        outputOf(callRaw("players.op", obj { put("name", JsonPrimitive(name)); put("deop", JsonPrimitive(deop)) }))

    suspend fun kick(name: String, reason: String = "") =
        outputOf(callRaw("players.kick", obj { put("name", JsonPrimitive(name)); put("reason", JsonPrimitive(reason)) }))

    /** Destructive — sends confirm:true. */
    suspend fun ban(name: String, reason: String = "") =
        outputOf(callRaw("players.ban", confirmed {
            put("name", JsonPrimitive(name)); put("reason", JsonPrimitive(reason))
        }))

    // ------------------------------------------------------------------ backups

    suspend fun backupList(): List<BackupEntry> =
        callRaw("backup.list")?.arrayField("backups")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(BackupEntry.serializer()), it) }
            ?: emptyList()

    suspend fun backupCreate(full: Boolean = false): BackupEntry? =
        callRaw("backup.create", obj { put("full", JsonPrimitive(full)) })
            ?.get("entry")?.takeIf { it != JsonNull }
            ?.let { McctlJson.decodeFromJsonElement(BackupEntry.serializer(), it) }

    suspend fun backupPrune(): Pair<List<String>, List<String>> {
        val r = callRaw("backup.prune") ?: return emptyList<String>() to emptyList()
        return r.stringList("kept") to r.stringList("removed")
    }

    suspend fun backupVerify(name: String): Boolean =
        callRaw("backup.verify", obj { put("name", JsonPrimitive(name)) })?.bool("ok") ?: false

    /** Destructive — sends confirm:true. The agent still refuses a running server. */
    suspend fun backupRestore(name: String): String? =
        callRaw("backup.restore", confirmed { put("name", JsonPrimitive(name)) })?.string("previous_world")

    // ------------------------------------------------------------------ logs

    suspend fun logsTail(lines: Int = 80): List<String> =
        callRaw("logs.tail", obj { put("lines", JsonPrimitive(lines)) })?.stringList("lines") ?: emptyList()

    suspend fun crashGet(name: String = ""): Pair<String, List<String>> {
        val r = callRaw("logs.tail", obj { put("crash", JsonPrimitive(true)); put("name", JsonPrimitive(name)) })
            ?: return "" to emptyList()
        return (r.string("name") ?: "") to r.stringList("lines")
    }

    suspend fun crashes(limit: Int = 15): List<CrashReport> =
        callRaw("logs.crashes", obj { put("limit", JsonPrimitive(limit)) })?.arrayField("crashes")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(CrashReport.serializer()), it) }
            ?: emptyList()

    suspend fun postmortem(crash: String = ""): Postmortem =
        decode(callRaw("postmortem", obj { put("crash", JsonPrimitive(crash)) }), Postmortem.serializer())

    // ------------------------------------------------------------------ metrics

    suspend fun metricsHistory(n: Int = 120): List<MetricSample> =
        callRaw("metrics.history", obj { put("n", JsonPrimitive(n)) })?.arrayField("samples")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(MetricSample.serializer()), it) }
            ?: emptyList()

    // ------------------------------------------------------------------ props / jvm

    suspend fun propsList(): Map<String, String?> {
        val props = callRaw("props.list")?.get("props") as? JsonObject ?: return emptyMap()
        return props.mapValues { (_, v) -> (v as? JsonPrimitive)?.contentOrNull }
    }

    suspend fun propsGet(key: String): String? =
        (callRaw("props.get", obj { put("key", JsonPrimitive(key)) })?.get("value") as? JsonPrimitive)?.contentOrNull

    /** Destructive — sends confirm:true. */
    suspend fun propsSet(key: String, value: String): String =
        callRaw("props.set", confirmed {
            put("key", JsonPrimitive(key)); put("value", JsonPrimitive(value))
        })?.string("value") ?: value

    suspend fun jvmShow(): JvmInfo = decode(callRaw("jvm.show"), JvmInfo.serializer())

    /** Destructive — sends confirm:true. */
    suspend fun jvmHeap(size: String) = ackOf(callRaw("jvm.heap", confirmed { put("size", JsonPrimitive(size)) }))

    // ------------------------------------------------------------------ mods / inspect

    suspend fun modsList(): List<ModInfo> =
        callRaw("mods.list")?.arrayField("mods")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(ModInfo.serializer()), it) }
            ?: emptyList()

    suspend fun inspect(section: String): InspectorSection =
        decode(callRaw("inspect", obj { put("section", JsonPrimitive(section)) }), InspectorSection.serializer())

    // ------------------------------------------------------------------ watchdog / events

    suspend fun watchdogState(): WatchdogState = decode(callRaw("watchdog.state"), WatchdogState.serializer())
    suspend fun watchdogArm(): WatchdogState = decode(callRaw("watchdog.arm"), WatchdogState.serializer())
    suspend fun watchdogDisarm(): WatchdogState = decode(callRaw("watchdog.disarm"), WatchdogState.serializer())

    suspend fun eventsList(since: Double? = null, limit: Int? = null): List<WatchdogEvent> =
        callRaw("events.list", obj {
            if (since != null) put("since", JsonPrimitive(since))
            if (limit != null) put("limit", JsonPrimitive(limit))
        })?.arrayField("events")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(WatchdogEvent.serializer()), it) }
            ?: emptyList()

    suspend fun eventsSubscribe(since: Double? = null) =
        callRaw("events.subscribe", obj { if (since != null) put("since", JsonPrimitive(since)) })

    suspend fun eventsUnsubscribe() = callRaw("events.unsubscribe")

    // ------------------------------------------------------------------ helpers

    private fun ackOf(r: JsonObject?): Boolean = r?.bool("ok") ?: true
    private fun outputOf(r: JsonObject?): String = r?.string("output") ?: ""

    private inline fun obj(build: JsonObjectBuilder.() -> Unit): JsonObject = buildJsonObject(build)

    private fun confirmed(extra: (JsonObjectBuilder.() -> Unit)? = null): JsonObject =
        buildJsonObject {
            put("confirm", JsonPrimitive(true))
            extra?.invoke(this)
        }
}

// --------------------------------------------------------------------- small JSON helpers

private fun buildJsonArrayOf(items: List<String>): JsonElement =
    kotlinx.serialization.json.JsonArray(items.map { JsonPrimitive(it) })

private fun JsonObject.string(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNull
private fun JsonObject.bool(key: String): Boolean? = (this[key] as? JsonPrimitive)?.booleanOrNull
private fun JsonObject.arrayField(key: String): JsonElement? = this[key]?.takeIf { it != JsonNull }
private fun JsonObject.stringList(key: String): List<String> =
    (this[key] as? kotlinx.serialization.json.JsonArray)?.map { it.jsonPrimitive.content } ?: emptyList()
