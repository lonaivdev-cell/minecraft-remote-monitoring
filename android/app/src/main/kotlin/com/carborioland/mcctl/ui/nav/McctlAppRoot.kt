package com.carborioland.mcctl.ui.nav

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.background
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.NavigationDrawerItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.carborioland.mcctl.data.ConnState
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.BiometricGate
import com.carborioland.mcctl.ui.LocalActionGate
import com.carborioland.mcctl.ui.LocalMessenger
import com.carborioland.mcctl.ui.components.HostKeyDialog
import com.carborioland.mcctl.ui.screens.AiScreen
import com.carborioland.mcctl.ui.screens.BackupsScreen
import com.carborioland.mcctl.ui.screens.ConnectScreen
import com.carborioland.mcctl.ui.screens.ConsoleScreen
import com.carborioland.mcctl.ui.screens.CrashesScreen
import com.carborioland.mcctl.ui.screens.DashboardScreen
import com.carborioland.mcctl.ui.screens.EventsScreen
import com.carborioland.mcctl.ui.screens.HistoryScreen
import com.carborioland.mcctl.ui.screens.InspectScreen
import com.carborioland.mcctl.ui.screens.JvmScreen
import com.carborioland.mcctl.ui.screens.LogsScreen
import com.carborioland.mcctl.ui.screens.ModsScreen
import com.carborioland.mcctl.ui.screens.PlayersScreen
import com.carborioland.mcctl.ui.screens.ProfilerScreen
import com.carborioland.mcctl.ui.screens.PropertiesScreen
import com.carborioland.mcctl.ui.screens.SettingsScreen
import com.carborioland.mcctl.ui.theme.PressStart
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun McctlAppRoot(container: AppContainer) {
    val navController = rememberNavController()
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }

    val connState by container.repository.state.collectAsStateWithLifecycle()
    val profile by container.profileStore.profile.collectAsStateWithLifecycle(initialValue = null)
    val hostKeyPrompt by container.repository.hostKeyPrompt.collectAsState()

    val backEntry by navController.currentBackStackEntryAsState()
    val current = Destination.fromRoute(backEntry?.destination?.route)
    val connected = connState is ConnState.Connected

    val activity = LocalContext.current as? FragmentActivity

    val messenger: (String) -> Unit = { msg -> scope.launch { snackbar.showSnackbar(msg) } }
    val actionGate: suspend (String) -> Boolean = gate@{ reason ->
        val p = profile
        if (p?.biometricForActions == true && activity != null) {
            BiometricGate.authenticate(activity, "Confirm action", reason)
        } else {
            true
        }
    }

    CompositionLocalProvider(LocalMessenger provides messenger, LocalActionGate provides actionGate) {
        ModalNavigationDrawer(
            drawerState = drawerState,
            drawerContent = {
                DrawerContent(
                    current = current,
                    connected = connected,
                    connLabel = profile?.label ?: "…",
                    onSelect = { dest ->
                        scope.launch { drawerState.close() }
                        navController.navigate(dest.route) {
                            launchSingleTop = true
                            popUpTo(Destination.START.route)
                        }
                    },
                )
            },
        ) {
            Scaffold(
                topBar = {
                    TopAppBar(
                        title = {
                            Text(
                                current.title,
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold,
                            )
                        },
                        navigationIcon = {
                            IconButton(onClick = { scope.launch { drawerState.open() } }) {
                                Icon(Icons.Filled.Menu, contentDescription = "Menu")
                            }
                        },
                        actions = {
                            ConnectionChip(connState)
                            if (connected) {
                                IconButton(onClick = { scope.launch { container.repository.refresh(false) } }) {
                                    Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                                }
                            }
                        },
                        colors = TopAppBarDefaults.topAppBarColors(
                            containerColor = MaterialTheme.colorScheme.surface,
                            titleContentColor = MaterialTheme.colorScheme.onSurface,
                        ),
                    )
                },
                snackbarHost = { SnackbarHost(snackbar) },
                containerColor = MaterialTheme.colorScheme.background,
            ) { padding ->
                AppNavHost(navController, container, padding)
            }
        }
    }

    hostKeyPrompt?.let { prompt ->
        HostKeyDialog(
            prompt = prompt,
            onAccept = { container.repository.resolveHostKey(true) },
            onReject = { container.repository.resolveHostKey(false) },
        )
    }
}

@Composable
private fun AppNavHost(
    navController: androidx.navigation.NavHostController,
    container: AppContainer,
    padding: PaddingValues,
) {
    NavHost(
        navController = navController,
        startDestination = Destination.START.route,
        modifier = Modifier.padding(padding).fillMaxSize(),
    ) {
        composable(Destination.Dashboard.route) { DashboardScreen(container) { navController.navigate(Destination.Connect.route) } }
        composable(Destination.History.route) { HistoryScreen(container) }
        composable(Destination.Console.route) { ConsoleScreen(container) }
        composable(Destination.Logs.route) { LogsScreen(container) }
        composable(Destination.Events.route) { EventsScreen(container) }
        composable(Destination.Players.route) { PlayersScreen(container) }
        composable(Destination.Backups.route) { BackupsScreen(container) }
        composable(Destination.Mods.route) { ModsScreen(container) }
        composable(Destination.Properties.route) { PropertiesScreen(container) }
        composable(Destination.Jvm.route) { JvmScreen(container) }
        composable(Destination.Crashes.route) { CrashesScreen(container) }
        composable(Destination.Inspect.route) { InspectScreen(container) }
        composable(Destination.Profiler.route) { ProfilerScreen(container) }
        composable(Destination.Ai.route) { AiScreen() }
        composable(Destination.Connect.route) {
            ConnectScreen(container) { navController.navigate(Destination.Dashboard.route) { popUpTo(Destination.Connect.route) { inclusive = true } } }
        }
        composable(Destination.Settings.route) { SettingsScreen(container) }
    }
}

@Composable
private fun DrawerContent(
    current: Destination,
    connected: Boolean,
    connLabel: String,
    onSelect: (Destination) -> Unit,
) {
    ModalDrawerSheet(drawerContainerColor = MaterialTheme.colorScheme.surface) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(28.dp).background(MaterialTheme.mc.grassFill))
                Spacer(Modifier.width(10.dp))
                Text(
                    "MCCTL",
                    style = androidx.compose.ui.text.TextStyle(fontFamily = PressStart, fontSize = 16.sp),
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
            Spacer(Modifier.size(4.dp))
            Text(connLabel, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
        }

        LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
            NavGroup.entries.forEach { group ->
                val dests = Destination.entries.filter { it.group == group }
                item(key = "h_${group.name}") {
                    Text(
                        group.label.uppercase(),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.mc.dim,
                        modifier = Modifier.padding(start = 12.dp, top = 14.dp, bottom = 4.dp),
                    )
                }
                items(dests, key = { it.route }) { dest ->
                    val enabled = !dest.requiresConnection || connected
                    NavigationDrawerItem(
                        icon = { Icon(dest.icon, contentDescription = null) },
                        label = { Text(dest.title, style = MaterialTheme.typography.bodyLarge) },
                        selected = dest == current,
                        onClick = { if (enabled) onSelect(dest) },
                        modifier = Modifier.padding(NavigationDrawerItemDefaults.ItemPadding),
                        colors = NavigationDrawerItemDefaults.colors(
                            selectedContainerColor = MaterialTheme.mc.grassDark.copy(alpha = 0.5f),
                            unselectedTextColor = if (enabled) MaterialTheme.colorScheme.onSurface else MaterialTheme.mc.dim.copy(alpha = 0.5f),
                            unselectedIconColor = if (enabled) MaterialTheme.colorScheme.onSurface else MaterialTheme.mc.dim.copy(alpha = 0.5f),
                        ),
                    )
                }
            }
        }
    }
}

@Composable
private fun ConnectionChip(state: ConnState) {
    val (label, color) = when (state) {
        is ConnState.Connected -> "LIVE" to MaterialTheme.mc.success
        is ConnState.Connecting -> "…" to MaterialTheme.mc.warning
        is ConnState.Failed -> "ERR" to MaterialTheme.mc.danger
        ConnState.Disconnected -> "OFF" to MaterialTheme.mc.dim
    }
    Box(
        Modifier
            .padding(end = 6.dp)
            .background(color.copy(alpha = 0.18f))
            .padding(horizontal = 10.dp, vertical = 5.dp),
    ) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = color)
    }
}
