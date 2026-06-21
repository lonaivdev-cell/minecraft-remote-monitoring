package com.carborioland.mcctl.core.model

/**
 * What a bulk offline sync still has to do, computed by diffing the server's [AssetCatalog]
 * against what the phone already holds. [toFetch] are the textures whose crc differs from the
 * local copy (missing or changed); [upToDate] is how many already match; [bytesToFetch] is the
 * download size of [toFetch] and [totalBytes] the size of the whole catalog — together they
 * drive a byte-accurate progress bar and an honest "12.3 / 47.8 MB" label.
 */
data class AssetSyncPlan(
    val toFetch: List<AssetCatalogEntry> = emptyList(),
    val upToDate: Int = 0,
    val bytesToFetch: Long = 0,
    val totalBytes: Long = 0,
) {
    val nothingToDo: Boolean get() = toFetch.isEmpty()
    val totalCount: Int get() = toFetch.size + upToDate
}

/**
 * Pure planner for the offline icon sync — the phone's half of the "properly sync files" contract.
 * The Python brain owns what *exists* (the catalog) and the *bytes*; only the phone knows what it
 * already has on disk, so the diff lives here, kept pure and tested in `:core`.
 */
object AssetSyncPlanner {
    /** Sentinel crc the phone stores for a texture the server reported as missing, so a re-sync
     *  doesn't keep asking for a PNG the pack doesn't ship. Never equals a real uint32 crc. */
    const val MISSING: Long = -1L

    /**
     * Diff [catalog] against [local] (texture id → cached crc, or [MISSING] for known-absent).
     * A texture is (re)fetched when the phone has no crc for it or the crc has changed. A texture
     * the server lists but that the phone already marked [MISSING] is only retried if the server's
     * crc now differs from [MISSING] — i.e. the pack started shipping it.
     */
    fun plan(catalog: AssetCatalog, local: Map<String, Long>): AssetSyncPlan {
        val toFetch = catalog.textures.filter { local[it.id] != it.crc }
        return AssetSyncPlan(
            toFetch = toFetch,
            upToDate = catalog.textures.size - toFetch.size,
            bytesToFetch = toFetch.sumOf { it.size },
            totalBytes = catalog.bytes,
        )
    }
}
