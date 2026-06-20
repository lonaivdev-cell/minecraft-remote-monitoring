package com.carborioland.mcctl.session

import android.app.Notification
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.carborioland.mcctl.push.McctlNotifications

/**
 * A foreground "session" service the [com.carborioland.mcctl.ui.ActionRunner] starts when an
 * action runs long (backup, restore, asset sync). It holds the app in the foreground with an
 * ongoing notification so Android won't reclaim the process — and the SSH/RPC channel with
 * it — mid-flight. Stopped as soon as the action finishes.
 *
 * It guards the work; it does not own the connection. Re-establishing a dropped SSH channel
 * still happens on the next call through [com.carborioland.mcctl.data.ServerRepository].
 */
class SessionService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val label = intent?.getStringExtra(EXTRA_LABEL) ?: "Working…"
        val notification = build(label)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, notification)
        }
        return START_NOT_STICKY
    }

    private fun build(label: String): Notification =
        NotificationCompat.Builder(this, McctlNotifications.CHANNEL_SESSION)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("mcctl session active")
            .setContentText(label)
            .setOngoing(true)
            .setContentIntent(McctlNotifications.launchIntent(this))
            .build()

    companion object {
        private const val NOTIF_ID = 4242
        private const val EXTRA_LABEL = "label"

        /** Best-effort: ignored if Android refuses a background FGS start (app not foreground). */
        fun start(context: Context, label: String) {
            val intent = Intent(context, SessionService::class.java).putExtra(EXTRA_LABEL, label)
            runCatching { ContextCompat.startForegroundService(context, intent) }
        }

        fun stop(context: Context) {
            runCatching { context.stopService(Intent(context, SessionService::class.java)) }
        }
    }
}
