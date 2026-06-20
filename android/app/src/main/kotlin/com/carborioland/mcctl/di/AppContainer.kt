package com.carborioland.mcctl.di

import android.content.Context
import com.carborioland.mcctl.data.ProfileStore
import com.carborioland.mcctl.data.ServerRepository
import com.carborioland.mcctl.data.security.SecureStore
import com.carborioland.mcctl.ui.IconCache
import com.carborioland.mcctl.ui.RecipeStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/**
 * Hand-rolled dependency container — small enough that a DI framework would be more
 * ceremony than it saves. Created once in [com.carborioland.mcctl.McctlApp] and handed to
 * the UI; ViewModels read what they need from it.
 */
class AppContainer(context: Context) {
    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val secureStore = SecureStore(context.applicationContext)
    val profileStore = ProfileStore(context.applicationContext)
    val repository = ServerRepository(appScope, secureStore)

    /** EMI item-icon cache: decoded PNGs from `icons.fetch`, persisted under the app cache dir. */
    val iconCache = IconCache(repository, java.io.File(context.applicationContext.cacheDir, "icons"))

    /** Lazily-synced recipe set powering EMI "what makes / what uses this". */
    val recipeStore = RecipeStore(repository)
}
