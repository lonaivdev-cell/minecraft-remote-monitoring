package com.carborioland.mcctl.push

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.carborioland.mcctl.data.ProfileStore
import com.carborioland.mcctl.data.PushStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.net.HttpURLConnection
import java.net.URL

/**
 * Background ntfy subscriber. WorkManager wakes this roughly every 15 min; it polls the
 * box's ntfy topic for messages newer than the stored cursor and raises each as a
 * notification. No Firebase, no extra infra — it reuses the v0.5.0 server-side `ntfy_*`
 * sink. The first run just records "now" so it never replays the topic's history.
 *
 * This is deliberately a poll (≤15 min latency). An instant push would mean UnifiedPush /
 * a held connection; that can layer on later behind the same settings.
 */
class NtfyPoller(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val profile = ProfileStore(applicationContext).profile.first()
        if (!profile.pushReady) return Result.success()   // disabled since the work was queued

        val store = PushStore(applicationContext)
        val since = store.since()
        val nowS = System.currentTimeMillis() / 1000
        if (since == 0L) {                                 // first poll: baseline, don't replay history
            store.setSince(nowS)
            return Result.success()
        }

        return try {
            val events = withContext(Dispatchers.IO) {
                fetch(profile.ntfyServer.trimEnd('/'), profile.ntfyTopic.trim(), since)
            }
            var latest = since
            for (e in events) {
                if (e.event != "message") continue
                McctlNotifications.postAlert(
                    applicationContext, e.id.hashCode(),
                    e.title.ifBlank { "CarborioLand" }, e.message,
                )
                if (e.time > latest) latest = e.time
            }
            if (latest > since) store.setSince(latest)
            Result.success()
        } catch (_: Exception) {
            Result.retry()                                  // transient network/server hiccup
        }
    }

    private fun fetch(server: String, topic: String, since: Long): List<NtfyEvent> {
        val url = URL("$server/$topic/json?poll=1&since=$since")
        val conn = (url.openConnection() as HttpURLConnection).apply {
            connectTimeout = 10_000
            readTimeout = 25_000
            requestMethod = "GET"
        }
        try {
            if (conn.responseCode !in 200..299) return emptyList()
            return conn.inputStream.bufferedReader().useLines { lines ->
                lines.mapNotNull { line ->
                    if (line.isBlank()) null
                    else runCatching { JSON.decodeFromString(NtfyEvent.serializer(), line) }.getOrNull()
                }.toList()
            }
        } finally {
            conn.disconnect()
        }
    }

    @Serializable
    data class NtfyEvent(
        val id: String = "",
        val time: Long = 0,
        val event: String = "",
        val topic: String = "",
        val title: String = "",
        val message: String = "",
    )

    private companion object {
        val JSON = Json { ignoreUnknownKeys = true }
    }
}
