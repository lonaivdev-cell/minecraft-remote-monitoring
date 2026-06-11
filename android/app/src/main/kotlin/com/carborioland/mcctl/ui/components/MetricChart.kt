package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.theme.mc
import kotlin.math.max

/**
 * A history chart card: a filled sparkline over a deepslate grid with min/avg/max/last
 * stats, mirroring the desktop GUI's metric cards. Nulls in [values] break the line, so a
 * server outage reads as a gap rather than a dive to zero.
 */
@Composable
fun MetricChartCard(
    title: String,
    values: List<Float?>,
    color: Color,
    modifier: Modifier = Modifier,
    fixedMax: Float? = null,
    format: (Float) -> String = { "%.1f".format(it) },
) {
    val present = values.filterNotNull()
    McPanel(modifier = modifier) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(title, style = MaterialTheme.typography.titleSmall, color = color)
            Text(
                present.lastOrNull()?.let(format) ?: "—",
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }

        // A fixed-max metric (TPS, %) is drawn from 0; an auto metric (players, load)
        // floats between its own min and max so small variations are still visible.
        val lo = present.minOrNull() ?: 0f
        val hi = present.maxOrNull() ?: 1f
        val baseline = if (fixedMax != null) 0f else lo
        val top = fixedMax ?: hi
        val span = max(top - baseline, 0.0001f)

        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(120.dp)
                .padding(top = 8.dp, bottom = 6.dp),
        ) {
            val w = size.width
            val h = size.height

            // grid: three faint horizontal rules
            val grid = Color(0xFF2E2E37)
            for (i in 0..3) {
                val y = h * i / 3f
                drawLine(grid, start = androidx.compose.ui.geometry.Offset(0f, y),
                    end = androidx.compose.ui.geometry.Offset(w, y), strokeWidth = 1f)
            }

            if (values.size < 2) return@Canvas
            val step = w / (values.size - 1).toFloat()
            fun x(i: Int) = i * step
            fun y(v: Float) = h - ((v - baseline) / span).coerceIn(0f, 1f) * h

            // filled area + line, breaking on nulls
            val line = Path()
            val area = Path()
            var penDown = false
            var firstX = 0f
            var lastX = 0f
            values.forEachIndexed { i, raw ->
                val v = raw
                if (v == null) {
                    if (penDown) { closeArea(area, lastX, firstX, h); drawArea(area, color); area.reset() }
                    penDown = false
                } else {
                    val px = x(i); val py = y(v)
                    if (!penDown) { line.moveTo(px, py); area.moveTo(px, h); area.lineTo(px, py); firstX = px; penDown = true }
                    else { line.lineTo(px, py); area.lineTo(px, py) }
                    lastX = px
                }
            }
            if (penDown) { closeArea(area, lastX, firstX, h); drawArea(area, color) }
            drawPath(line, color, style = Stroke(width = 3f))
        }

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Stat("min", present.minOrNull(), format)
            Stat("avg", present.average().takeIf { present.isNotEmpty() }?.toFloat(), format)
            Stat("max", present.maxOrNull(), format)
        }
    }
}

private fun closeArea(area: Path, lastX: Float, firstX: Float, h: Float) {
    area.lineTo(lastX, h)
    area.lineTo(firstX, h)
    area.close()
}

private fun androidx.compose.ui.graphics.drawscope.DrawScope.drawArea(area: Path, color: Color) {
    drawPath(area, color.copy(alpha = 0.18f))
}

@Composable
private fun Stat(label: String, value: Float?, format: (Float) -> String) {
    Row {
        Text("$label ", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim)
        Text(value?.let(format) ?: "—", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface)
    }
}
