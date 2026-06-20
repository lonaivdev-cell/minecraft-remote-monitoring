package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.IconCache
import com.carborioland.mcctl.ui.theme.mc

/**
 * One item's icon, EMI-style. Reads the decoded bitmap from [cache] (which the screen has
 * batch-prefetched) and draws it crisp with nearest-neighbour [FilterQuality.None] — Minecraft
 * textures are 16px pixel art and must never be smoothed. While the icon is loading, or when the
 * pack ships no texture for it, it shows a stone placeholder with the item's initial.
 */
@Composable
fun ItemIcon(
    textureId: String,
    cache: IconCache,
    modifier: Modifier = Modifier,
    size: Dp = 36.dp,
    fallbackLabel: String = "",
) {
    val bmp = cache.bitmap(textureId)
    Box(modifier.size(size), contentAlignment = Alignment.Center) {
        if (bmp != null) {
            Image(
                bitmap = bmp,
                contentDescription = null,
                filterQuality = FilterQuality.None,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Box(Modifier.fillMaxSize().background(Color(0xFF24242B)), contentAlignment = Alignment.Center) {
                fallbackLabel.firstOrNull()?.uppercaseChar()?.let {
                    Text(it.toString(), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.dim)
                }
            }
        }
    }
}
