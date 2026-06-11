package com.carborioland.mcctl.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.carborioland.mcctl.R

/**
 * Three SIL OFL pixel fonts give the app a Minecraft-adjacent retro look (the licenses
 * ship under assets/licenses):
 *  - PressStart2P — chunky arcade caps, for badges/headings/buttons (used sparingly: it's
 *    wide, so only short labels).
 *  - Silkscreen   — a clean pixel face for titles and section labels.
 *  - VT323        — a CRT-terminal monospace, perfect for console, logs and IDs.
 */
val PressStart = FontFamily(Font(R.font.press_start_2p, FontWeight.Normal))

val Silkscreen = FontFamily(
    Font(R.font.silkscreen, FontWeight.Normal),
    Font(R.font.silkscreen_bold, FontWeight.Bold),
)

val Terminal = FontFamily(Font(R.font.vt323, FontWeight.Normal))

/**
 * Material typography mapped onto the pixel fonts. PressStart is reserved for the largest,
 * shortest labels; Silkscreen carries titles and body; VT323 is opted into directly where
 * a monospace reads best (it has its own [TerminalTextStyle]).
 */
val McctlTypography = Typography(
    displaySmall = TextStyle(fontFamily = PressStart, fontSize = 20.sp, lineHeight = 30.sp, letterSpacing = 1.sp),
    headlineMedium = TextStyle(fontFamily = PressStart, fontSize = 15.sp, lineHeight = 24.sp, letterSpacing = 1.sp),
    headlineSmall = TextStyle(fontFamily = PressStart, fontSize = 12.sp, lineHeight = 20.sp, letterSpacing = 0.5.sp),
    titleLarge = TextStyle(fontFamily = Silkscreen, fontWeight = FontWeight.Bold, fontSize = 22.sp, lineHeight = 28.sp),
    titleMedium = TextStyle(fontFamily = Silkscreen, fontWeight = FontWeight.Bold, fontSize = 17.sp, lineHeight = 24.sp),
    titleSmall = TextStyle(fontFamily = Silkscreen, fontWeight = FontWeight.Bold, fontSize = 14.sp, lineHeight = 20.sp),
    bodyLarge = TextStyle(fontFamily = Silkscreen, fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontFamily = Silkscreen, fontSize = 14.sp, lineHeight = 21.sp),
    bodySmall = TextStyle(fontFamily = Silkscreen, fontSize = 12.sp, lineHeight = 18.sp),
    labelLarge = TextStyle(fontFamily = PressStart, fontSize = 11.sp, lineHeight = 16.sp, letterSpacing = 0.5.sp),
    labelMedium = TextStyle(fontFamily = Silkscreen, fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 0.5.sp),
    labelSmall = TextStyle(fontFamily = Silkscreen, fontSize = 11.sp, letterSpacing = 0.5.sp),
)

/** Monospace style for console output, log tails, fingerprints and the public key. */
val TerminalTextStyle = TextStyle(fontFamily = Terminal, fontSize = 18.sp, lineHeight = 20.sp)
