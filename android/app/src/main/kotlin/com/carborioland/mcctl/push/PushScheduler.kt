package com.carborioland.mcctl.push

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.carborioland.mcctl.data.ConnectionProfile
import java.util.concurrent.TimeUnit

/** Owns the WorkManager schedule for the ntfy poller — enable, disable, or kick it once. */
object PushScheduler {
    private const val WORK = "mcctl_ntfy_poll"

    /** Reconcile the schedule with the profile: enqueue when push is ready, cancel otherwise. */
    fun apply(context: Context, profile: ConnectionProfile) {
        val wm = WorkManager.getInstance(context)
        if (!profile.pushReady) {
            wm.cancelUniqueWork(WORK)
            return
        }
        val req = PeriodicWorkRequestBuilder<NtfyPoller>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        wm.enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    /** Fire one immediate poll (e.g. right after the user saves push settings). */
    fun pollNow(context: Context) {
        val req = OneTimeWorkRequestBuilder<NtfyPoller>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueue(req)
    }
}
