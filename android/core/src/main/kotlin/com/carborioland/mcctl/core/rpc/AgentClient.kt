package com.carborioland.mcctl.core.rpc

import com.carborioland.mcctl.core.model.BackupEntry
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.ConfigContent
import com.carborioland.mcctl.core.model.ConfigFile
import com.carborioland.mcctl.core.model.CraftPlan
import com.carborioland.mcctl.core.model.CraftResult
import com.carborioland.mcctl.core.model.CrashReport
import com.carborioland.mcctl.core.model.HealthReport
import com.carborioland.mcctl.core.model.HelloResult
import com.carborioland.mcctl.core.model.IconBatch
import com.carborioland.mcctl.core.model.InspectorSection
import com.carborioland.mcctl.core.model.ItemManifest
import com.carborioland.mcctl.core.model.JvmInfo
import com.carborioland.mcctl.core.model.MetricSample
import com.carborioland.mcctl.core.model.ModInfo
import com.carborioland.mcctl.core.model.PlayerList
import com.carborioland.mcctl.core.model.Postmortem
import com.carborioland.mcctl.core.model.Recipe
import com.carborioland.mcctl.core.model.RecipeSearch
import com.carborioland.mcctl.core.model.Status
import com.carborioland.mcctl.core.model.TagItems
import com.carborioland.mcctl.core.model.TpsReport
import com.carborioland.mcctl.core.model.VanillaSync
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

    // ------------------------------------------------------------------ recipes / crafting

    /**
     * Search recipes (id/result substring) across every category — crafting, the cook family
     * (smelting/blasting/smoking/campfire), stonecutting and smithing — from jars + datapacks.
     * [offset] skips that many matches first, so the browser can page the whole pack into a cache.
     */
    suspend fun recipesSearch(
        query: String,
        limit: Int = 60,
        offset: Int = 0,
        craftable: Boolean = false,
        player: String = "",
    ): RecipeSearch =
        decode(callRaw("recipes.search", obj {
            put("query", JsonPrimitive(query)); put("limit", JsonPrimitive(limit))
            put("offset", JsonPrimitive(offset))
            if (craftable) put("craftable", JsonPrimitive(true))
            if (player.isNotBlank()) put("player", JsonPrimitive(player))
        }), RecipeSearch.serializer())

    /**
     * Recipe-tree cost: the total base materials + leftovers to craft [count] of a recipe,
     * recursively expanding craftable intermediates (EMI-style). Pure server-side.
     */
    suspend fun recipesCost(id: String, count: Int = 1, maxDepth: Int = 64): CostBreakdown =
        decode(callRaw("recipes.cost", obj {
            put("id", JsonPrimitive(id)); put("count", JsonPrimitive(count))
            put("max_depth", JsonPrimitive(maxDepth))
        }), CostBreakdown.serializer())

    /** One recipe by exact id (e.g. "minecraft:chest"). */
    suspend fun recipeGet(id: String): Recipe =
        callRaw("recipes.get", obj { put("id", JsonPrimitive(id)) })
            ?.get("recipe")?.takeIf { it != JsonNull }
            ?.let { McctlJson.decodeFromJsonElement(Recipe.serializer(), it) }
            ?: throw RpcException(RpcCodes.APP, "no recipe in response")

    /** Resolve a `#tag` ingredient (e.g. "minecraft:planks") to its concrete item ids. */
    suspend fun recipesTag(tag: String): TagItems =
        decode(callRaw("recipes.tag", obj { put("tag", JsonPrimitive(tag)) }), TagItems.serializer())

    // ------------------------------------------------------------------ items / icons

    /**
     * The EMI-style item index — id → display name → icon texture id — from the vanilla jar +
     * mod jars + resourcepacks. Page with [offset]/[limit]; pass each entry's `icon` to
     * [iconsFetch] for the PNG. [query] filters by id or display name (case-insensitive).
     */
    suspend fun itemsManifest(query: String = "", limit: Int = 2000, offset: Int = 0): ItemManifest =
        decode(callRaw("items.manifest", obj {
            put("query", JsonPrimitive(query)); put("limit", JsonPrimitive(limit))
            put("offset", JsonPrimitive(offset))
        }), ItemManifest.serializer())

    /**
     * Fetch icon PNGs by texture id (from [itemsManifest]); the wire's base64 is decoded to raw
     * bytes for `BitmapFactory`. Cache by texture id and only request the ones not cached yet.
     */
    suspend fun iconsFetch(textures: List<String>): IconBatch {
        if (textures.isEmpty()) return IconBatch()
        val r = callRaw("icons.fetch", obj { put("textures", buildJsonArrayOf(textures)) })
        val icons = (r?.get("icons") as? JsonObject).orEmpty().mapValues { (_, v) ->
            java.util.Base64.getDecoder().decode((v as JsonPrimitive).content)
        }
        return IconBatch(icons, r?.stringList("missing") ?: emptyList())
    }

    /**
     * Download the matching vanilla client jar onto the server (cached, sha1-verified) so vanilla
     * items gain icons + names. Needs the `actions` capability. [version] empty = auto-detect.
     */
    suspend fun assetsSync(version: String = "", force: Boolean = false): VanillaSync =
        decode(callRaw("assets.sync", obj {
            if (version.isNotBlank()) put("version", JsonPrimitive(version))
            put("force", JsonPrimitive(force))
        }), VanillaSync.serializer())

    /**
     * Plan a craft against live inventory — no mutation. [count] null = hold-to-max;
     * empty [source]/[receiver] fall back to the server's `[crafting]` config.
     */
    suspend fun craftPreview(
        id: String,
        count: Int?,
        source: String = "",
        receiver: String = "",
        includeStored: Boolean? = null,
    ): CraftPlan =
        decode(callRaw("craft.preview", obj {
            put("id", JsonPrimitive(id))
            put("count", count?.let { JsonPrimitive(it) } ?: JsonNull)
            if (source.isNotBlank()) put("source", JsonPrimitive(source))
            if (receiver.isNotBlank()) put("receiver", JsonPrimitive(receiver))
            if (includeStored != null) put("include_stored", JsonPrimitive(includeStored))
        }), CraftPlan.serializer())

    /**
     * Craft for real: consume inputs (/clear) + grant the output (/give). Needs the `actions`
     * capability and is destructive, so it sends confirm:true — gate the user first. [count]
     * null = hold-to-max (one output stack).
     */
    suspend fun craftDo(id: String, count: Int?, source: String = "", receiver: String = ""): CraftResult =
        decode(callRaw("craft.do", confirmed {
            put("id", JsonPrimitive(id))
            put("count", count?.let { JsonPrimitive(it) } ?: JsonNull)
            if (source.isNotBlank()) put("source", JsonPrimitive(source))
            if (receiver.isNotBlank()) put("receiver", JsonPrimitive(receiver))
        }), CraftResult.serializer())

    // ------------------------------------------------------------------ mod configs

    suspend fun configTree(mods: Boolean = true): List<ConfigFile> =
        callRaw("config.tree", obj { put("mods", JsonPrimitive(mods)) })?.arrayField("files")
            ?.let { McctlJson.decodeFromJsonElement(ListSerializer(ConfigFile.serializer()), it) }
            ?: emptyList()

    suspend fun configGet(path: String): ConfigContent =
        decode(callRaw("config.get", obj { put("path", JsonPrimitive(path)) }), ConfigContent.serializer())

    /**
     * Write a config file. Destructive — sends confirm:true; confirm with the user first.
     * `reload=true` also runs /reload on a live server. Returns a short, honest status line.
     */
    suspend fun configSet(path: String, text: String, reload: Boolean = true): String {
        val r = callRaw("config.set", confirmed {
            put("path", JsonPrimitive(path)); put("text", JsonPrimitive(text)); put("reload", JsonPrimitive(reload))
        })
        val running = r?.bool("running") ?: false
        val reloaded = r?.bool("reloaded") ?: false
        return when {
            !running -> "Saved — .bak kept; loads on next start"
            reloaded -> "Saved — .bak kept, /reload run; cached values need a restart"
            else -> "Saved — .bak kept; live-reload where the mod supports it"
        }
    }

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
