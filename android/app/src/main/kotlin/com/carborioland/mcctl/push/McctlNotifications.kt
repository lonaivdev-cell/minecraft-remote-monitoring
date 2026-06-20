package com.carborioland.mcctl.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.carborioland.mcctl.MainActivity

/**
 * Notification plumbing for the two background surfaces: ntfy push **alerts** (high
 * importance, one per message) and the foreground **session** notification shown while a
 * long action holds the SSH connection alive. Channels are created once at app start.
 */
object McctlNotifications {
    const val CHANNEL_ALERTS = "mcctl_alerts"
    const val CHANNEL_SESSION = "mcctl_session"

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, "Server alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Watchdog alerts pushed from the box via ntfy."
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_SESSION, "Active session", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while a long action keeps the SSH session alive."
            },
        )
    }

    /** A PendingIntent that opens the app — used by both the alert and session notifications. */
    fun launchIntent(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        return PendingIntent.getActivity(
            context, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    /** Post one ntfy alert (no-op if the user has notifications switched off). */
    fun postAlert(context: Context, id: Int, title: String, body: String) {
        val n = NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(title.ifBlank { "CarborioLand" })
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(launchIntent(context))
            .build()
        val mgr = NotificationManagerCompat.from(context)
        if (mgr.areNotificationsEnabled()) mgr.notify(id, n)
    }
}
