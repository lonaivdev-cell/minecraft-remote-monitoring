package com.carborioland.mcctl.assets

import android.content.Context
import com.carborioland.mcctl.core.model.AssetSyncPlanner
import com.carborioland.mcctl.core.rpc.RpcCodes
import com.carborioland.mcctl.core.rpc.RpcException
import com.carborioland.mcctl.data.ServerRepository
import com.carborioland.mcctl.session.SessionService
import com.carborioland.mcctl.ui.IconCache
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext

/**
 * Observable state of the bulk offline asset sync, for the progress bar to render.
 */
sealed interface AssetSyncState {
    data object Idle : AssetSyncState

    /** A sync in flight. Progress is byte-accurate against the plan; [fraction] drives the bar. */
    data class Running(
        val phase: Phase,
        val doneBytes: Long = 0,
        val totalBytes: Long = 0,
        val doneCount: Int = 0,
        val totalCount: Int = 0,
    ) : AssetSyncState {
        val fraction: Float
            get() = if (totalBytes > 0) (doneBytes.toFloat() / totalBytes).coerceIn(0f, 1f) else 0f
    }

    /** Finished: [fetched] downloaded, [upToDate] already current, [missing] not shipped by the pack. */
    data class Done(val fetched: Int, val upToDate: Int, val missing: Int, val bytes: Long) : AssetSyncState

    data class Failed(val message: String) : AssetSyncState

    enum class Phase(val label: String) {
        INDEXING("Indexing items…"),
        CATALOGING("Checking the server catalog…"),
        DOWNLOADING("Downloading icons"),
    }
}

/**
 * Drives a full "download every item icon for offline use" sync, with a progress [state] the UI
 * observes. App-scoped and single-flight: it survives screen changes, and starting again while a
 * sync runs is a no-op. The actual work is split brain/face per the project's design — the Python
 * core owns the catalog (what exists + crc) and the bytes; [AssetSyncPlanner] (pure, in `:core`)
 * decides what's missing; this only orchestrates the round-trips and reports progress.
 *
 * The sync is **idempotent and resumable**: it fetches only textures whose crc differs from the
 * local cache, persists each batch as it lands, and re-running after an interruption picks up the
 * remainder. A long download is promoted to a foreground [SessionService] so Android keeps the SSH
 * channel alive while the screen is backgrounded — exactly like a long [com.carborioland.mcctl.ui.ActionRunner] action.
 */
class AssetSyncManager(
    private val repository: ServerRepository,
    private val iconCache: IconCache,
    private val scope: CoroutineScope,
    private val appContext: Context,
) {
    private val _state = MutableStateFlow<AssetSyncState>(AssetSyncState.Idle)
    val state: StateFlow<AssetSyncState> = _state.asStateFlow()

    private var job: Job? = null

    val running: Boolean get() = job?.isActive == true

    /** Begin (or no-op if already running). Safe to call from the UI thread. */
    fun start() {
        if (running) return
        job = scope.launch {
            var promoted = false
            // Quick syncs (everything already cached) finish before this fires, so no notification flashes.
            val promote = launch {
                delay(PROMOTE_AFTER_MS)
                SessionService.start(appContext, "Downloading item assets")
                promoted = true
            }
            try {
                runSync()
            } catch (e: CancellationException) {
                _state.value = AssetSyncState.Idle
                throw e
            } catch (e: Exception) {
                _state.value = AssetSyncState.Failed(e.message ?: "asset sync failed")
            } finally {
                promote.cancel()
                if (promoted) SessionService.stop(appContext)
            }
        }
    }

    fun cancel() {
        job?.cancel()
    }

    private suspend fun runSync() {
        // 1. Index — page items.manifest in full and persist it for instant, offline cold starts.
        //    Keep the entries: they're the texture set for the no-catalog fallback below.
        _state.value = AssetSyncState.Running(AssetSyncState.Phase.INDEXING)
        val items = iconCache.syncIndex()

        // 2. Catalog — ask the brain for every icon's crc + size, then diff against the local cache.
        //    An older agent has no `assets.catalog` (the method is optional — it only optimizes the
        //    sync with crc diffing + a byte-accurate bar); fall back to a manifest-derived plan so a
        //    server that predates the method still syncs instead of failing hard.
        _state.value = AssetSyncState.Running(AssetSyncState.Phase.CATALOGING)
        val local = iconCache.localCatalog()
        val plan = try {
            val catalog = withContext(Dispatchers.IO) { repository.requireClient().assetsCatalog() }
            AssetSyncPlanner.plan(catalog, local)
        } catch (e: RpcException) {
            if (e.code != RpcCodes.METHOD_NOT_FOUND) throw e
            AssetSyncPlanner.planFromManifest(items, local)
        }

        // 3. Download — fetch only the missing/changed textures, batched, persisting as we go. With a
        //    catalog the bar is byte-accurate; in the fallback (totalBytes == 0) we tally the bytes
        //    actually fetched, so the count drives the bar and the "this run" total stays honest.
        val byteAccurate = plan.totalBytes > 0
        var doneBytes = 0L
        var doneCount = 0
        var missing = 0
        _state.value = AssetSyncState.Running(
            AssetSyncState.Phase.DOWNLOADING, doneBytes = 0L, totalBytes = plan.bytesToFetch,
            doneCount = 0, totalCount = plan.toFetch.size,
        )
        for (batch in plan.toFetch.chunked(BATCH)) {
            coroutineContext.ensureActive()
            val fetched = withContext(Dispatchers.IO) {
                repository.requireClient().iconsFetch(batch.map { it.id })
            }
            missing += iconCache.persistBatch(batch, fetched.icons)
            doneBytes += if (byteAccurate) batch.sumOf { it.size }
                         else fetched.icons.values.sumOf { it.size.toLong() }
            doneCount += batch.size
            _state.value = AssetSyncState.Running(
                AssetSyncState.Phase.DOWNLOADING, doneBytes, plan.bytesToFetch, doneCount, plan.toFetch.size,
            )
        }

        _state.value = AssetSyncState.Done(
            fetched = plan.toFetch.size - missing,
            upToDate = plan.upToDate,
            missing = missing,
            bytes = doneBytes,
        )
    }

    private companion object {
        const val BATCH = 200            // icons per icons.fetch round-trip (server caps at 500)
        const val PROMOTE_AFTER_MS = 2_500L
    }
}
