package com.carborioland.mcctl.ui.components

import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * The pixel-art 3D edge that defines the Minecraft button/panel look: a flat [fill] with
 * a bright top-left edge and a dark bottom-right edge, wrapped in a 1px black outline.
 * When [pressed] the edges swap, so the surface reads as pushed in — exactly how the
 * game's buttons feel.
 */
fun Modifier.pixelBevel(
    fill: Color,
    light: Color,
    dark: Color,
    thickness: Dp = 3.dp,
    pressed: Boolean = false,
    outline: Color = Color(0xFF0A0A0C),
): Modifier = drawBehind {
    val t = thickness.toPx()
    val o = 1.5f
    val topLeft = if (pressed) dark else light
    val bottomRight = if (pressed) light else dark

    drawRect(outline) // outer 1px black outline
    drawRect(fill, topLeft = Offset(o, o), size = Size(size.width - 2 * o, size.height - 2 * o))
    drawRect(topLeft, topLeft = Offset(o, o), size = Size(size.width - 2 * o, t))                       // top
    drawRect(topLeft, topLeft = Offset(o, o), size = Size(t, size.height - 2 * o))                       // left
    drawRect(bottomRight, topLeft = Offset(o, size.height - o - t), size = Size(size.width - 2 * o, t))  // bottom
    drawRect(bottomRight, topLeft = Offset(size.width - o - t, o), size = Size(t, size.height - 2 * o))  // right
}
