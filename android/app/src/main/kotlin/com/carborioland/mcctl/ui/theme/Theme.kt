package com.carborioland.mcctl.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

/**
 * Minecraft-specific colours that Material's [androidx.compose.material3.ColorScheme]
 * has no slot for: the success/warning/danger semantics the status badge needs, the gem
 * accents, and the light/dark bevel shades that give panels and buttons their pixel-3D
 * edges. Reached through [MaterialTheme.mc].
 */
data class McctlColors(
    val online: Color,
    val booting: Color,
    val offline: Color,
    val unreachable: Color,
    val success: Color,
    val warning: Color,
    val danger: Color,
    val info: Color,
    val gold: Color,
    val dim: Color,
    // Bevel materials: (fill, lightEdge, darkEdge) for each button/panel surface.
    val stoneFill: Color,
    val stoneLight: Color,
    val stoneDark: Color,
    val grassFill: Color,
    val grassLight: Color,
    val grassDark: Color,
    val dangerFill: Color,
    val dangerLight: Color,
    val dangerDark: Color,
    val panelFill: Color,
    val panelLight: Color,
    val panelDark: Color,
)

private val DarkMc = McctlColors(
    online = Emerald,
    booting = GoldXp,
    offline = StoneLight,
    unreachable = Redstone,
    success = Emerald,
    warning = GoldXp,
    danger = Redstone,
    info = Diamond,
    gold = GoldXp,
    dim = BoneDim,
    stoneFill = Stone,
    stoneLight = StoneLight,
    stoneDark = StoneDark,
    grassFill = Grass,
    grassLight = GrassLight,
    grassDark = GrassDark,
    dangerFill = Redstone,
    dangerLight = Color(0xFFF06A63),
    dangerDark = RedstoneDark,
    panelFill = DeepslateSurface,
    panelLight = DeepslateSurfaceHi,
    panelDark = Color(0xFF101015),
)

val LocalMcctlColors = staticCompositionLocalOf { DarkMc }

private val DarkScheme = darkColorScheme(
    primary = Grass,
    onPrimary = InkOnGrass,
    primaryContainer = GrassDark,
    onPrimaryContainer = BoneWhite,
    secondary = Diamond,
    onSecondary = Color(0xFF06231F),
    tertiary = GoldXp,
    onTertiary = Color(0xFF231A00),
    error = Redstone,
    onError = Color(0xFF240605),
    background = DeepslateBg,
    onBackground = BoneWhite,
    surface = DeepslateSurface,
    onSurface = BoneWhite,
    surfaceVariant = DeepslateSurfaceHi,
    onSurfaceVariant = BoneDim,
    outline = DeepslateLine,
)

/** Accessor for the Minecraft palette: `MaterialTheme.mc.online`, etc. */
val MaterialTheme.mc: McctlColors
    @Composable @ReadOnlyComposable
    get() = LocalMcctlColors.current

@Composable
fun McctlTheme(
    @Suppress("UNUSED_PARAMETER") darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    // The Minecraft look is intentionally always dark (deepslate); there is no light scheme.
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            window.statusBarColor = DeepslateBg.toArgb()
            window.navigationBarColor = DeepslateBg.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = false
        }
    }
    androidx.compose.runtime.CompositionLocalProvider(LocalMcctlColors provides DarkMc) {
        MaterialTheme(colorScheme = DarkScheme, typography = McctlTypography, content = content)
    }
}
