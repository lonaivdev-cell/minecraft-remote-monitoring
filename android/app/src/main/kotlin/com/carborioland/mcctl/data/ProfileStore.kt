package com.carborioland.mcctl.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.Json

private val Context.profileDataStore: DataStore<Preferences> by preferencesDataStore(name = "mcctl_profile")

/** Persists the (non-secret) [ConnectionProfile] via DataStore as a small JSON blob. */
class ProfileStore(private val context: Context) {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val key = stringPreferencesKey("profile_json")

    val profile: Flow<ConnectionProfile> = context.profileDataStore.data.map { prefs ->
        prefs[key]?.let { runCatching { json.decodeFromString<ConnectionProfile>(it) }.getOrNull() }
            ?: ConnectionProfile()
    }

    suspend fun save(profile: ConnectionProfile) {
        context.profileDataStore.edit { it[key] = json.encodeToString(profile) }
    }
}
