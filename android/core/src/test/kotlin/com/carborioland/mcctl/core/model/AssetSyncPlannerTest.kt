package com.carborioland.mcctl.core.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AssetSyncPlannerTest {

    private fun catalog(vararg entries: Triple<String, Long, Long>) =
        AssetCatalog(
            textures = entries.map { (id, crc, size) -> AssetCatalogEntry(id, crc, size) },
            count = entries.size,
            bytes = entries.sumOf { it.third },
        )

    @Test
    fun `an empty local cache fetches everything`() {
        val cat = catalog(
            Triple("a:item/x", 1L, 100L),
            Triple("a:item/y", 2L, 250L),
        )
        val plan = AssetSyncPlanner.plan(cat, emptyMap())
        assertEquals(2, plan.toFetch.size)
        assertEquals(0, plan.upToDate)
        assertEquals(350L, plan.bytesToFetch)
        assertEquals(350L, plan.totalBytes)
        assertEquals(2, plan.totalCount)
        assertFalse(plan.nothingToDo)
    }

    @Test
    fun `a fully matching cache has nothing to do`() {
        val cat = catalog(Triple("a:item/x", 1L, 100L), Triple("a:item/y", 2L, 250L))
        val plan = AssetSyncPlanner.plan(cat, mapOf("a:item/x" to 1L, "a:item/y" to 2L))
        assertTrue(plan.nothingToDo)
        assertEquals(2, plan.upToDate)
        assertEquals(0L, plan.bytesToFetch)
    }

    @Test
    fun `a changed crc is re-fetched but matching ones are skipped`() {
        val cat = catalog(Triple("a:item/x", 1L, 100L), Triple("a:item/y", 99L, 250L))
        // x unchanged, y's content changed under the same texture id (resource-pack swap)
        val plan = AssetSyncPlanner.plan(cat, mapOf("a:item/x" to 1L, "a:item/y" to 2L))
        assertEquals(listOf("a:item/y"), plan.toFetch.map { it.id })
        assertEquals(1, plan.upToDate)
        assertEquals(250L, plan.bytesToFetch)
    }

    @Test
    fun `a texture marked missing is not retried until the pack ships it`() {
        val cat = catalog(Triple("a:item/x", AssetSyncPlanner.MISSING, 0L))
        // server still reports it absent (crc == MISSING) -> already recorded, skip
        val skip = AssetSyncPlanner.plan(cat, mapOf("a:item/x" to AssetSyncPlanner.MISSING))
        assertTrue(skip.nothingToDo)
        // now the pack actually ships it (real crc) -> fetch it
        val now = catalog(Triple("a:item/x", 7L, 64L))
        val fetch = AssetSyncPlanner.plan(now, mapOf("a:item/x" to AssetSyncPlanner.MISSING))
        assertEquals(listOf("a:item/x"), fetch.toFetch.map { it.id })
    }

    // --- planFromManifest: the fallback when the server's agent predates assets.catalog ---

    private fun item(id: String, icon: String) = ItemEntry(id = id, name = id, icon = icon)

    @Test
    fun `manifest fallback fetches every distinct icon when nothing is cached`() {
        val plan = AssetSyncPlanner.planFromManifest(
            listOf(
                item("a:stick", "a:item/stick"),
                item("a:chest", "a:block/chest"),
                item("a:trapped_chest", "a:block/chest"),   // shares a texture -> de-duplicated
                item("a:mystery", ""),                       // no icon resolved -> skipped
            ),
            emptyMap(),
        )
        assertEquals(listOf("a:item/stick", "a:block/chest"), plan.toFetch.map { it.id })
        assertEquals(0, plan.upToDate)
        // no catalog -> no crc or size, so progress is count-based, not byte-accurate
        assertEquals(0L, plan.bytesToFetch)
        assertEquals(0L, plan.totalBytes)
        assertTrue(plan.toFetch.all { it.crc == AssetSyncPlanner.UNKNOWN && it.size == 0L })
    }

    @Test
    fun `manifest fallback skips anything already recorded so a re-run resumes`() {
        val items = listOf(
            item("a:stick", "a:item/stick"),     // already cached (real crc)
            item("a:chest", "a:block/chest"),    // recorded missing on a prior run
            item("a:torch", "a:item/torch"),     // fetched by an earlier fallback run
            item("a:new", "a:item/new"),         // genuinely new -> the only fetch
        )
        val local = mapOf(
            "a:item/stick" to 123L,
            "a:block/chest" to AssetSyncPlanner.MISSING,
            "a:item/torch" to AssetSyncPlanner.UNKNOWN,
        )
        val plan = AssetSyncPlanner.planFromManifest(items, local)
        assertEquals(listOf("a:item/new"), plan.toFetch.map { it.id })
        assertEquals(3, plan.upToDate)
        assertTrue(plan.toFetch.single().crc == AssetSyncPlanner.UNKNOWN)
    }
}
