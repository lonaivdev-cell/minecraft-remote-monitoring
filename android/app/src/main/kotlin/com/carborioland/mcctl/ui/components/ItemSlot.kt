package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carborioland.mcctl.ui.IconCache
import com.carborioland.mcctl.ui.theme.mc

/**
 * A Minecraft inventory slot: a recessed pixel-bevel well holding one item's icon, drawn crisp
 * (nearest-neighbour) and resolved from an *item id* through [IconCache] (which loads the
 * manifest's item→texture index once). An empty [itemId] renders just the well — exactly the
 * hole in a chest recipe. A [count] > 1 stamps the stack number in the corner like the game; a
 * non-null [onClick] makes the slot the EMI pivot point ("show me this item's recipes").
 */
@Composable
fun ItemSlot(
    itemId: String,
    cache: IconCache,
    modifier: Modifier = Modifier,
    size: Dp = 46.dp,
    count: Int = 0,
    onClick: (() -> Unit)? = null,
) {
    LaunchedEffect(itemId) { if (itemId.isNotBlank()) cache.ensureItems(listOf(itemId)) }
    val tappable = onClick != null && itemId.isNotBlank()
    Box(
        modifier
            .size(size)
            .pixelBevel(Color(0xFF17171C), Color(0xFF33333D), Color(0xFF0A0A0D), thickness = 2.dp, pressed = true)
            .then(if (tappable) Modifier.clickable { onClick!!() } else Modifier),
        contentAlignment = Alignment.Center,
    ) {
        if (itemId.isNotBlank()) {
            val bmp = cache.bitmapForItem(itemId)
            if (bmp != null) {
                Image(
                    bitmap = bmp,
                    contentDescription = itemId,
                    filterQuality = FilterQuality.None,
                    modifier = Modifier.fillMaxSize().padding(5.dp),
                )
            } else {
                Text(slotInitial(itemId), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.dim)
            }
            if (count > 1) {
                Text(
                    "$count",
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold, fontSize = 10.sp),
                    color = Color.White,
                    modifier = Modifier.align(Alignment.BottomEnd).padding(end = 3.dp, bottom = 1.dp),
                )
            }
        }
    }
}

private fun slotInitial(itemId: String): String =
    itemId.substringAfter(':').substringAfterLast('/').firstOrNull()?.uppercaseChar()?.toString() ?: "?"
