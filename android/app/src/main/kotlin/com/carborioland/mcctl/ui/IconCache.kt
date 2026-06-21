package com.carborioland.mcctl.ui

import android.graphics.BitmapFactory
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import com.carborioland.mcctl.core.model.AssetCatalogEntry
import com.carborioland.mcctl.core.model.AssetSyncPlanner
import com.carborioland.mcctl.core.model.ItemEntry
import com.carborioland.mcctl.data.ServerRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.Json
import java.io.File

/**
 * App-scoped icon store for the EMI item browser — the phone's half of "bundle every PNG EMI
 * reads". It does four things:
 *
 *  - **resolves** an item id → its icon *texture id* via the item index ([loadIndex]); so a
 *    recipe (which speaks in item ids) can show icons. The index is persisted to disk, so a cold
 *    start is instant and offline instead of re-scanning the server's jars over SSH each launch.
 *  - **fetches + decodes** texture PNGs in batches over `icons.fetch`, into [ImageBitmap]s.
 *  - **persists** the raw PNG bytes under the app cache dir, plus a CRC sidecar ([localCatalog]),
 *    so once an icon has been pulled it renders offline forever — and a re-sync re-fetches only
 *    the textures whose content actually changed (a resource-pack swap), not the whole pack.
 *  - **bulk-stores** what [com.carborioland.mcctl.assets.AssetSyncManager] downloads
 *    ([persistBatch]), without decoding every PNG into memory.
 *
 * Snapshot-backed: a screen that reads [bitmap]/[bitmapForItem] recomposes the instant a batch
 * lands. A cached `null` bitmap means "the pack ships no texture" — placeholder, never retried.
 * The Python brain already did the hard part (item→texture resolution, mod/vanilla de-dup,
 * crc digests); this only caches what it returns.
 */
class IconCache(
    private val repository: ServerRepository,
    private val diskDir: File?,
) {
    private val bitmaps = mutableStateMapOf<String, ImageBitmap?>()   // textureId -> bitmap (null = missing)
    private val itemTexture = HashMap<String, String>()              // itemId -> textureId
    private val itemName = HashMap<String, String>()                 // itemId -> display name
    @Volatile private var indexLoaded = false
    private val inFlight = HashSet<String>()
    private val lock = Mutex()
    private val indexLock = Mutex()

    // texId -> crc currently on disk (AssetSyncPlanner.MISSING = server has no PNG for it).
    private var localCrc: HashMap<String, Long>? = null
    private val catalogLock = Mutex()

    private val json = Json { ignoreUnknownKeys = true }

    /** The decoded icon for a texture id, or null if missing / not loaded yet. */
    fun bitmap(textureId: String): ImageBitmap? = bitmaps[textureId]

    /** The decoded icon for an item id (resolved through the manifest index), or null. */
    fun bitmapForItem(itemId: String): ImageBitmap? = itemTexture[itemId]?.let { bitmaps[it] }

    /** The texture id an item resolves to, once [loadIndex] has run. */
    fun textureOf(itemId: String): String? = itemTexture[itemId]

    /** The display name for an item id, once [loadIndex] has run. */
    fun nameOf(itemId: String): String? = itemName[itemId]

    // -------------------------------------------------------------------- index

    /** Load the item→texture index once: disk first (instant, offline), else paged over RPC. */
    suspend fun loadIndex() {
        if (indexLoaded) return
        indexLock.withLock {
            if (indexLoaded) return
            val disk = withContext(Dispatchers.IO) { readIndexDisk() }
            if (disk != null) {
                applyIndex(disk)
                indexLoaded = true
                return
            }
            runCatching {
                val items = pageManifest()
                applyIndex(items)
                withContext(Dispatchers.IO) { writeIndexDisk(items) }   // persist so next start is offline
            }.onSuccess { indexLoaded = true }
        }
    }

    /**
     * Re-page the whole item index from the server and persist it — the index half of a bulk sync.
     * Unlike [loadIndex] this always hits the server, so a changed pack's new items are picked up.
     * Returns the entries (id + texture) so the sync knows the full texture set.
     */
    suspend fun syncIndex(): List<ItemEntry> {
        val items = pageManifest()
        applyIndex(items)
        withContext(Dispatchers.IO) { writeIndexDisk(items) }
        indexLoaded = true
        return items
    }

    private suspend fun pageManifest(): List<ItemEntry> {
        val client = repository.requireClient()
        val all = ArrayList<ItemEntry>()
        var offset = 0
        while (true) {
            val page = withContext(Dispatchers.IO) { client.itemsManifest("", limit = 1000, offset = offset) }
            if (page.items.isEmpty()) break
            all += page.items
            offset += page.items.size
            if (!page.truncated) break
        }
        return all
    }

    private fun applyIndex(items: List<ItemEntry>) {
        for (e in items) {
            if (e.icon.isNotBlank()) itemTexture[e.id] = e.icon
            if (e.name.isNotBlank()) itemName[e.id] = e.name
        }
    }

    // ------------------------------------------------------------------ fetching

    /** Fetch + decode any of [textureIds] not already cached or in flight — disk first, then RPC. */
    suspend fun ensure(textureIds: Collection<String>) {
        val want = ArrayList<String>()
        lock.withLock {
            for (id in textureIds) {
                if (id.isNotBlank() && id !in bitmaps && id !in inFlight) {
                    inFlight += id
                    want += id
                }
            }
        }
        if (want.isEmpty()) return
        try {
            val decoded = withContext(Dispatchers.IO) {
                val out = HashMap<String, ImageBitmap?>(want.size)
                val need = ArrayList<String>()
                for (id in want) {
                    val bytes = readDisk(id)
                    if (bytes != null) out[id] = decode(bytes) else need += id
                }
                if (need.isNotEmpty()) {
                    val batch = repository.requireClient().iconsFetch(need)
                    for (id in need) {
                        val bytes = batch.icons[id]
                        if (bytes != null) {
                            writeDisk(id, bytes)
                            out[id] = decode(bytes)
                        } else {
                            out[id] = null      // pack has no texture for this id
                        }
                    }
                }
                out
            }
            bitmaps.putAll(decoded)
        } catch (_: Exception) {
            // Leave them uncached so a later pass can retry (e.g. after assets.sync).
        } finally {
            lock.withLock { inFlight.removeAll(want.toHashSet()) }
        }
    }

    /** Resolve item ids to their textures (loading the index if needed) and fetch those icons. */
    suspend fun ensureItems(itemIds: Collection<String>) {
        if (!indexLoaded) loadIndex()
        ensure(itemIds.mapNotNull { itemTexture[it] })
    }

    // --------------------------------------------------------------- bulk sync

    /** The crc of every texture cached on disk (loaded once), for the offline-sync diff. A copy,
     *  so a concurrent [persistBatch] can't mutate it mid-read. */
    suspend fun localCatalog(): Map<String, Long> = catalogLock.withLock { HashMap(loadCrc()) }

    /**
     * Persist one batch the bulk sync downloaded: write each present PNG and record its crc; record
     * the server-reported-missing ones with the [AssetSyncPlanner.MISSING] sentinel so a re-sync
     * doesn't keep asking for them. The CRC sidecar is flushed to disk so an interrupted sync
     * resumes where it left off. Does not decode PNGs (the browser decodes lazily on demand), but
     * evicts any stale in-memory bitmap so a changed icon refreshes. Returns the count not found.
     */
    suspend fun persistBatch(entries: List<AssetCatalogEntry>, fetched: Map<String, ByteArray>): Int {
        var missing = 0
        catalogLock.withLock {
            val crc = loadCrc()
            withContext(Dispatchers.IO) {
                for (e in entries) {
                    val bytes = fetched[e.id]
                    if (bytes != null) {
                        writeDisk(e.id, bytes)
                        crc[e.id] = e.crc
                    } else {
                        crc[e.id] = AssetSyncPlanner.MISSING
                        missing++
                    }
                }
                writeCrcDisk(crc)
            }
            for (e in entries) if (e.id in fetched) bitmaps.remove(e.id)   // drop stale decode
        }
        return missing
    }

    // ----------------------------------------------------------------- clearing

    /**
     * Forget the item index (memory + disk) so the next [loadIndex]/[syncIndex] re-pages it from
     * the server — used after `assets.sync` adds vanilla items. Cached PNGs are kept (still valid).
     */
    fun invalidateIndex() {
        itemTexture.clear()
        itemName.clear()
        indexLoaded = false
        runCatching { indexFile()?.delete() }
    }

    /** Wipe everything on disk and in memory — the "free up space" action. */
    suspend fun clearOffline() {
        catalogLock.withLock {
            withContext(Dispatchers.IO) {
                diskDir?.listFiles()?.forEach { runCatching { it.delete() } }
            }
            localCrc = HashMap()
        }
        bitmaps.clear()
        invalidateIndex()
    }

    /** (count, bytes) of icon PNGs currently cached on disk, for the Settings summary. */
    suspend fun diskUsage(): Pair<Int, Long> = withContext(Dispatchers.IO) {
        val pngs = diskDir?.listFiles { f -> f.isFile && f.name.endsWith(".png") } ?: return@withContext 0 to 0L
        pngs.size to pngs.sumOf { it.length() }
    }

    // ------------------------------------------------------------------- disk io

    private fun decode(bytes: ByteArray): ImageBitmap? =
        runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap() }.getOrNull()

    private fun safeName(textureId: String): String = textureId.replace(Regex("[^A-Za-z0-9._-]"), "_")

    private fun diskFile(textureId: String): File? = diskDir?.let { File(it, "${safeName(textureId)}.png") }

    private fun readDisk(textureId: String): ByteArray? =
        diskFile(textureId)?.takeIf { it.isFile }?.let { runCatching { it.readBytes() }.getOrNull() }

    private fun writeDisk(textureId: String, bytes: ByteArray) {
        val f = diskFile(textureId) ?: return
        runCatching { f.parentFile?.mkdirs(); f.writeBytes(bytes) }
    }

    // Sidecars live alongside the PNGs; the leading "_" can't collide with a sanitized texture id
    // (those always carry a namespace + slash, e.g. "minecraft_item_stick").
    private fun indexFile(): File? = diskDir?.let { File(it, "_manifest-index.json") }
    private fun catalogFile(): File? = diskDir?.let { File(it, "_icon-catalog.json") }

    private fun readIndexDisk(): List<ItemEntry>? {
        val f = indexFile()?.takeIf { it.isFile } ?: return null
        return runCatching {
            json.decodeFromString(ListSerializer(ItemEntry.serializer()), f.readText())
        }.getOrNull()?.takeIf { it.isNotEmpty() }
    }

    private fun writeIndexDisk(items: List<ItemEntry>) {
        val f = indexFile() ?: return
        runCatching {
            f.parentFile?.mkdirs()
            f.writeText(json.encodeToString(ListSerializer(ItemEntry.serializer()), items))
        }
    }

    /** Load the crc sidecar into [localCrc] once. Caller holds [catalogLock]. */
    private fun loadCrc(): HashMap<String, Long> {
        localCrc?.let { return it }
        val f = catalogFile()
        val loaded = if (f != null && f.isFile) {
            runCatching { json.decodeFromString(MapSerializer(String.serializer(), Long.serializer()), f.readText()) }
                .getOrNull()
        } else null
        return HashMap(loaded ?: emptyMap()).also { localCrc = it }
    }

    private fun writeCrcDisk(crc: Map<String, Long>) {
        val f = catalogFile() ?: return
        runCatching {
            f.parentFile?.mkdirs()
            f.writeText(json.encodeToString(MapSerializer(String.serializer(), Long.serializer()), crc))
        }
    }
}
