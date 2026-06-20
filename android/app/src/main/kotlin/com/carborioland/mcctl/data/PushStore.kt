package com.carborioland.mcctl.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.pushDataStore: DataStore<Preferences> by preferencesDataStore(name = "mcctl_push")

/** Remembers the ntfy `since` cursor so the background poller only sees new messages. */
class PushStore(private val context: Context) {

    private val sinceKey = longPreferencesKey("ntfy_since_s")

    /** Unix seconds of the newest message already delivered (0 = never polled). */
    suspend fun since(): Long = context.pushDataStore.data.map { it[sinceKey] ?: 0L }.first()

    suspend fun setSince(value: Long) {
        context.pushDataStore.edit { it[sinceKey] = value }
    }
}
