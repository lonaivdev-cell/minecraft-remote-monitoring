package com.carborioland.mcctl.ui.nav

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Archive
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Biotech
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Extension
import androidx.compose.material.icons.filled.GridView
import androidx.compose.material.icons.filled.Group
import androidx.compose.material.icons.filled.Hub
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.NotificationsActive
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material.icons.filled.Timeline
import androidx.compose.material.icons.filled.Tune
import androidx.compose.ui.graphics.vector.ImageVector

/** Logical grouping for the navigation drawer, mirroring the desktop's page order. */
enum class NavGroup(val label: String) {
    Monitor("Monitor"),
    Manage("Manage"),
    System("System"),
    App("App"),
}

/**
 * Every screen in the app. [requiresConnection] destinations are disabled until a session
 * is live; [route] is the NavHost key. The order here is the drawer order.
 */
enum class Destination(
    val route: String,
    val title: String,
    val icon: ImageVector,
    val group: NavGroup,
    val requiresConnection: Boolean = true,
) {
    Dashboard("dashboard", "Overview", Icons.Filled.Dashboard, NavGroup.Monitor),
    History("history", "History", Icons.Filled.Timeline, NavGroup.Monitor),
    Console("console", "Console", Icons.Filled.Terminal, NavGroup.Monitor),
    Logs("logs", "Logs", Icons.Filled.Description, NavGroup.Monitor),
    Events("events", "Events", Icons.Filled.NotificationsActive, NavGroup.Monitor),

    Players("players", "Players", Icons.Filled.Group, NavGroup.Manage),
    Backups("backups", "Backups", Icons.Filled.Archive, NavGroup.Manage),
    Mods("mods", "Mods", Icons.Filled.Extension, NavGroup.Manage),
    Crafting("crafting", "Crafting", Icons.Filled.GridView, NavGroup.Manage),
    ModConfigs("modconfigs", "Mod Configs", Icons.Filled.Edit, NavGroup.Manage),
    Properties("properties", "Properties", Icons.Filled.Tune, NavGroup.Manage),
    Jvm("jvm", "JVM", Icons.Filled.Memory, NavGroup.Manage),

    Crashes("crashes", "Crashes", Icons.Filled.BugReport, NavGroup.System),
    Inspect("inspect", "Inspect", Icons.Filled.Biotech, NavGroup.System),
    Profiler("profiler", "Profiler", Icons.Filled.Speed, NavGroup.System),
    Ai("ai", "AI", Icons.Filled.AutoAwesome, NavGroup.System),

    Connect("connect", "Connection", Icons.Filled.Hub, NavGroup.App, requiresConnection = false),
    Settings("settings", "Settings", Icons.Filled.Settings, NavGroup.App, requiresConnection = false),
    ;

    companion object {
        val START = Dashboard
        fun fromRoute(route: String?): Destination = entries.firstOrNull { it.route == route } ?: Dashboard
    }
}
