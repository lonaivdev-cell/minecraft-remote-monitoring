package com.carborioland.mcctl.util

import kotlin.math.abs

/** Presentation helpers mirroring the Python `util.human_*` formatters. */
object Format {

    fun bytes(n: Long?): String {
        if (n == null) return "—"
        var v = n.toDouble()
        val units = listOf("B", "KB", "MB", "GB", "TB", "PB")
        var i = 0
        while (abs(v) >= 1024 && i < units.lastIndex) {
            v /= 1024.0
            i++
        }
        return if (i == 0) "${n} ${units[0]}" else "%.1f %s".format(v, units[i])
    }

    fun duration(seconds: Int?): String {
        if (seconds == null) return "—"
        if (seconds < 0) return "—"
        val d = seconds / 86400
        val h = (seconds % 86400) / 3600
        val m = (seconds % 3600) / 60
        val s = seconds % 60
        return when {
            d > 0 -> "${d}d ${h}h"
            h > 0 -> "${h}h ${m}m"
            m > 0 -> "${m}m ${s}s"
            else -> "${s}s"
        }
    }

    fun durationLong(seconds: Double?): String =
        if (seconds == null) "—" else duration(seconds.toInt())

    fun tps(v: Double?): String = if (v == null) "—" else "%.1f".format(v)
}
