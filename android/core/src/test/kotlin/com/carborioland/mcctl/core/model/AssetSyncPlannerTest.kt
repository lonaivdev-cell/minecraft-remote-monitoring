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
}
