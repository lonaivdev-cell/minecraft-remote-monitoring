package com.carborioland.mcctl.ui

import com.carborioland.mcctl.core.model.Recipe
import com.carborioland.mcctl.core.model.RecipeIndex
import com.carborioland.mcctl.data.ServerRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * The phone's loaded recipe set, EMI-style: synced once (paged over `recipes.search("")`) and
 * kept for the session, then queried entirely client-side via [RecipeIndex] for "what makes this"
 * and "what uses this". Single-flight and idempotent, so the first screen that needs it pays the
 * sync and every later lookup is instant. The brain already de-dups newest-pack-wins; this only
 * accumulates the pages.
 */
class RecipeStore(private val repository: ServerRepository) {
    @Volatile private var index: RecipeIndex? = null
    private val mutex = Mutex()

    /** The index if already synced, else null — for a screen to decide whether to show progress. */
    fun cached(): RecipeIndex? = index

    /** Sync the whole recipe set (once) and return the index. Safe to call concurrently. */
    suspend fun ensureLoaded(): RecipeIndex {
        index?.let { return it }
        return mutex.withLock {
            index ?: run {
                val all = ArrayList<Recipe>()
                val client = repository.requireClient()
                var offset = 0
                while (true) {
                    val page = withContext(Dispatchers.IO) { client.recipesSearch("", limit = 1000, offset = offset) }
                    if (page.recipes.isEmpty()) break
                    all += page.recipes
                    offset += page.recipes.size
                    if (!page.truncated || all.size > 50_000) break   // cap a pathological pack
                }
                RecipeIndex(all).also { index = it }
            }
        }
    }

    fun invalidate() {
        index = null
    }
}
