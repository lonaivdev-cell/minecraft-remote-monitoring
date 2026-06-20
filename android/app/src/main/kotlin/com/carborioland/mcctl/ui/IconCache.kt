package com.carborioland.mcctl.ui

import android.graphics.BitmapFactory
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import com.carborioland.mcctl.data.ServerRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File

/**
 * App-scoped icon store for the EMI item browser — the phone's half of "bundle every PNG EMI
 * reads". It does three things:
 *
 *  - **resolves** an item id → its icon *texture id* via a one-time, paged load of
 *    `items.manifest` ([loadIndex]); so a recipe (which speaks in item ids) can show icons.
 *  - **fetches + decodes** texture PNGs in batches over `icons.fetch`, into [ImageBitmap]s.
 *  - **persists** the raw PNG bytes under the app cache dir, so once an icon has been pulled it
 *    renders offline forever (until [clear]).
 *
 * Snapshot-backed: a screen that reads [bitmap]/[bitmapForItem] recomposes the instant a batch
 * lands. A cached `null` bitmap means "the pack ships no texture" — placeholder, never retried.
 * The Python brain already did the hard part (item→texture resolution, mod/vanilla de-dup); this
 * only caches what it returns.
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

    /** The decoded icon for a texture id, or null if missing / not loaded yet. */
    fun bitmap(textureId: String): ImageBitmap? = bitmaps[textureId]

    /** The decoded icon for an item id (resolved through the manifest index), or null. */
    fun bitmapForItem(itemId: String): ImageBitmap? = itemTexture[itemId]?.let { bitmaps[it] }

    /** The texture id an item resolves to, once [loadIndex] has run. */
    fun textureOf(itemId: String): String? = itemTexture[itemId]

    /** The display name for an item id, once [loadIndex] has run. */
    fun nameOf(itemId: String): String? = itemName[itemId]

    /** Load the whole item→texture index once (paged), so recipe item ids can show icons. */
    suspend fun loadIndex() {
        if (indexLoaded) return
        indexLock.withLock {
            if (indexLoaded) return
            try {
                val client = repository.requireClient()
                var offset = 0
                while (true) {
                    val page = withContext(Dispatchers.IO) { client.itemsManifest("", limit = 1000, offset = offset) }
                    if (page.items.isEmpty()) break
                    for (e in page.items) {
                        if (e.icon.isNotBlank()) itemTexture[e.id] = e.icon
                        if (e.name.isNotBlank()) itemName[e.id] = e.name
                    }
                    offset += page.items.size
                    if (!page.truncated) break
                }
                indexLoaded = true
            } catch (_: Exception) {
                // Leave it unloaded so a later screen retries.
            }
        }
    }

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

    /** Drop everything — memory + the item index — e.g. after `assets.sync` adds vanilla textures. */
    fun clear() {
        bitmaps.clear()
        itemTexture.clear()
        itemName.clear()
        indexLoaded = false
    }

    private fun decode(bytes: ByteArray): ImageBitmap? =
        runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap() }.getOrNull()

    private fun diskFile(textureId: String): File? {
        val dir = diskDir ?: return null
        val safe = textureId.replace(Regex("[^A-Za-z0-9._-]"), "_")
        return File(dir, "$safe.png")
    }

    private fun readDisk(textureId: String): ByteArray? =
        diskFile(textureId)?.takeIf { it.isFile }?.let { runCatching { it.readBytes() }.getOrNull() }

    private fun writeDisk(textureId: String, bytes: ByteArray) {
        val f = diskFile(textureId) ?: return
        runCatching { f.parentFile?.mkdirs(); f.writeBytes(bytes) }
    }
}
