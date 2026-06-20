package com.carborioland.mcctl.ui.components

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.theme.mc

/**
 * The craft button with the pack's signature gesture: a quick **tap** crafts one, a
 * **press-and-hold** past [holdMs] crafts the maximum (hold-to-max). While held, a charge
 * bar sweeps across the button and the label switches to [holdLabel]; at the threshold it
 * fires [onHold] with a haptic pulse (and won't also fire a tap on release). Releasing
 * early fires [onTap]. Honors `[crafting].hold_ms` — the server-configured threshold the
 * `craft.preview` plan carries.
 */
@Composable
fun HoldCraftButton(
    label: String,
    holdLabel: String,
    holdMs: Int,
    enabled: Boolean,
    onTap: () -> Unit,
    onHold: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = MaterialTheme.mc
    val haptics = LocalHapticFeedback.current
    val progress = remember { Animatable(0f) }
    var pressed by remember { mutableStateOf(false) }
    var firedHold by remember { mutableStateOf(false) }

    // Drive the charge while held; fire hold-to-max if it reaches full before release.
    LaunchedEffect(pressed, holdMs) {
        if (pressed) {
            firedHold = false
            progress.snapTo(0f)
            // Cancelled (an early release flips `pressed`) -> this just stops; the tap is
            // fired by the gesture handler instead.
            progress.animateTo(1f, tween(durationMillis = holdMs.coerceAtLeast(1), easing = LinearEasing))
            firedHold = true
            haptics.performHapticFeedback(HapticFeedbackType.LongPress)
            onHold()
        } else {
            progress.animateTo(0f, tween(durationMillis = 160))
        }
    }

    val charging = pressed && progress.value > 0.02f

    Box(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 58.dp)
            .pixelBevel(
                fill = if (enabled) c.grassFill else Color(0xFF44444B),
                light = if (enabled) c.grassLight else Color(0xFF55555C),
                dark = if (enabled) c.grassDark else Color(0xFF2A2A30),
                pressed = pressed && enabled,
            )
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectTapGestures(
                    onPress = {
                        pressed = true
                        val released = tryAwaitRelease()
                        val wasHold = firedHold
                        pressed = false
                        if (released && !wasHold) onTap()
                    },
                )
            }
            .drawBehind {
                val p = progress.value
                if (p > 0f) {
                    val inset = 3.dp.toPx()
                    val barH = 6.dp.toPx()
                    // a faint full sweep + a solid bottom bar that reads as a hold timer
                    drawRect(
                        color = Color(0xFFCDECA0).copy(alpha = 0.22f),
                        topLeft = Offset(inset, inset),
                        size = Size((size.width - 2 * inset) * p, size.height - 2 * inset),
                    )
                    drawRect(
                        color = Color(0xFFE8FFC2),
                        topLeft = Offset(inset, size.height - inset - barH),
                        size = Size((size.width - 2 * inset) * p, barH),
                    )
                }
            }
            .padding(horizontal = 16.dp, vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = (if (charging) holdLabel else label).uppercase(),
            style = MaterialTheme.typography.labelLarge,
            color = if (enabled) Color(0xFF15240B) else Color(0xFF8C8C8C),
            textAlign = TextAlign.Center,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
