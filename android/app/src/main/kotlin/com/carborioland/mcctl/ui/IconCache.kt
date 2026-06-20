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

/**
 * App-scoped icon store for the EMI item browser. Maps an icon *texture id* (from
 * `items.manifest`) to its decoded [ImageBitmap], fetching PNGs in batches over `icons.fetch`
 * and caching them for the session so a grid scrolls without re-fetching. Snapshot-backed, so a
 * screen that reads [bitmap] recomposes the instant a batch lands.
 *
 * A cached `null` means "fetched, but the pack ships no texture" — drawn as a placeholder and
 * never retried; a texture id simply absent from the map hasn't been loaded yet. The brain
 * already resolved item → texture and de-duplicated across mods/vanilla, so this only caches.
 */
class IconCache(private val repository: ServerRepository) {
    private val bitmaps = mutableStateMapOf<String, ImageBitmap?>()
    private val inFlight = HashSet<String>()
    private val lock = Mutex()

    /** The decoded icon, or null if missing / not loaded yet. Safe to read in composition. */
    fun bitmap(textureId: String): ImageBitmap? = bitmaps[textureId]

    /** Fetch + decode any of [textureIds] not already cached or in flight — batched, one RPC. */
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
                val batch = repository.requireClient().iconsFetch(want)
                want.associateWith { id -> batch.icons[id]?.let(::decode) }
            }
            bitmaps.putAll(decoded)
        } catch (_: Exception) {
            // Leave them uncached so a later pass can retry (e.g. after assets.sync).
        } finally {
            lock.withLock { inFlight.removeAll(want.toHashSet()) }
        }
    }

    /** Drop everything — e.g. after `assets.sync` adds vanilla textures — to force a re-fetch. */
    fun clear() {
        bitmaps.clear()
    }

    private fun decode(bytes: ByteArray): ImageBitmap? =
        runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap() }.getOrNull()
}
