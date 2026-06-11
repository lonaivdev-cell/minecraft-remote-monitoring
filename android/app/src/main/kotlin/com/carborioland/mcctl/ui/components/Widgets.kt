package com.carborioland.mcctl.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carborioland.mcctl.ui.theme.PressStart
import com.carborioland.mcctl.ui.theme.Silkscreen
import com.carborioland.mcctl.ui.theme.mc

/** The button materials — each maps to a (fill, light, dark) bevel triple. */
enum class BtnKind { Primary, Neutral, Danger, Gold }

/**
 * A blocky Minecraft button. It depresses on touch (the bevel inverts and the label nudges
 * down), greys out when disabled, and uses the chunky pixel font for short labels.
 */
@Composable
fun McButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    kind: BtnKind = BtnKind.Neutral,
    enabled: Boolean = true,
    large: Boolean = false,
) {
    val c = MaterialTheme.mc
    val (fill, light, dark) = when (kind) {
        BtnKind.Primary -> Triple(c.grassFill, c.grassLight, c.grassDark)
        BtnKind.Neutral -> Triple(c.stoneFill, c.stoneLight, c.stoneDark)
        BtnKind.Danger -> Triple(c.dangerFill, c.dangerLight, c.dangerDark)
        BtnKind.Gold -> Triple(c.gold, Color(0xFFFFE08A), Color(0xFFC9991F))
    }
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val grey = !enabled

    Box(
        modifier = modifier
            .heightIn(min = 48.dp)
            .pixelBevel(
                fill = if (grey) Color(0xFF44444B) else fill,
                light = if (grey) Color(0xFF55555C) else light,
                dark = if (grey) Color(0xFF2A2A30) else dark,
                pressed = pressed && enabled,
            )
            .clickable(interactionSource = interaction, indication = null, enabled = enabled) { onClick() }
            .padding(horizontal = 16.dp, vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = text.uppercase(),
            style = MaterialTheme.typography.labelLarge,
            fontSize = if (large) 13.sp else 11.sp,
            color = if (grey) Color(0xFF8C8C8C) else labelColorFor(kind),
            textAlign = TextAlign.Center,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.graphicsLayer { translationY = if (pressed && enabled) 2f else 0f },
        )
    }
}

private fun labelColorFor(kind: BtnKind): Color = when (kind) {
    BtnKind.Primary -> Color(0xFF15240B)
    BtnKind.Gold -> Color(0xFF231A00)
    else -> Color(0xFFF3EFE6)
}

/** A beveled deepslate container — the app's card/panel surface. */
@Composable
fun McPanel(
    modifier: Modifier = Modifier,
    content: @Composable Column.() -> Unit,
) {
    val c = MaterialTheme.mc
    Column(
        modifier = modifier
            .fillMaxWidth()
            .pixelBevel(c.panelFill, c.panelLight, c.panelDark, thickness = 2.dp)
            .padding(14.dp),
        content = content,
    )
}

/** A pixel-font section label with a small grass tick, used as a group header. */
@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    val c = MaterialTheme.mc
    Row(modifier = modifier.padding(top = 4.dp, bottom = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp).background(c.grassFill))
        Spacer(Modifier.width(8.dp))
        Text(text.uppercase(), style = MaterialTheme.typography.headlineSmall, color = c.dim)
    }
}

/** A thin pixel divider line. */
@Composable
fun PixelDivider(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().height(2.dp).background(Color(0xFF000000).copy(alpha = 0.35f)))
}

/** A label/value line, value in bold so numbers and ids stand out. */
@Composable
fun KeyValue(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    valueColor: Color? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim)
        Spacer(Modifier.width(12.dp))
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium.copy(fontFamily = Silkscreen, fontWeight = FontWeight.Bold),
            color = valueColor ?: MaterialTheme.colorScheme.onSurface,
            textAlign = TextAlign.End,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * A segmented gauge, drawn as discrete pixel blocks (very Minecraft). [fraction] in 0..1;
 * [tint] colours the filled blocks. Used for heap, RAM and TPS.
 */
@Composable
fun PixelGauge(
    fraction: Float,
    modifier: Modifier = Modifier,
    tint: Color = MaterialTheme.mc.success,
    segments: Int = 20,
    height: Dp = 18.dp,
) {
    val filled = (fraction.coerceIn(0f, 1f) * segments).toInt()
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(height)
            .pixelBevel(Color(0xFF101015), Color(0xFF2A2A33), Color(0xFF000000), thickness = 1.5.dp)
            .padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        repeat(segments) { i ->
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .background(if (i < filled) tint else Color(0xFF24242B)),
            )
        }
    }
}

/** A large status pill with a coloured bevel — the dashboard's headline state. */
@Composable
fun StatusBadge(text: String, color: Color, modifier: Modifier = Modifier) {
    val animated by animateColorAsState(color, label = "badge")
    Box(
        modifier = modifier
            .pixelBevel(animated.copy(alpha = 0.22f), animated.copy(alpha = 0.5f), Color(0xFF000000), thickness = 3.dp)
            .padding(horizontal = 26.dp, vertical = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text.uppercase(), style = TextStyle(fontFamily = PressStart, fontSize = 18.sp), color = animated)
    }
}
