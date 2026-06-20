package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowForward
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.Recipe
import com.carborioland.mcctl.ui.IconCache
import com.carborioland.mcctl.ui.theme.mc

/**
 * An EMI-style recipe card: the inputs laid out the way the game shows them — a positional
 * crafting grid, a furnace line for the cook family, or the input row for stonecutting/smithing —
 * an arrow, and the result slot with its stack count. Every input slot and legend row is tappable
 * ([onPickItem]) so the browser pivots EMI-style: "show me *this* item's recipes." Icons resolve
 * from item ids through [cache]; the brain already did the resolution, this only draws it.
 */
@Composable
fun RecipeView(
    recipe: Recipe,
    cache: IconCache,
    modifier: Modifier = Modifier,
    onPickItem: (String) -> Unit = {},
) {
    LaunchedEffect(recipe.id) {
        val ids = buildList {
            add(recipe.resultItem)
            addAll(recipe.grid.filter { it.isNotBlank() })
            recipe.ingredients.forEach { ing -> ing.options.firstOrNull()?.let { add(it) } }
        }
        cache.ensureItems(ids)
    }

    McPanel(modifier) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            CategoryBadge(recipe.category, recipe.type)
            Spacer(Modifier.width(8.dp))
            Text(
                cache.nameOf(recipe.resultItem) ?: prettyItem(recipe.resultItem),
                style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
            )
        }

        Spacer(Modifier.height(12.dp))
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (recipe.hasGrid) CraftingGrid(recipe, cache, onPickItem) else InputRow(recipe, cache, onPickItem)
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(Icons.Filled.ArrowForward, contentDescription = "makes", tint = MaterialTheme.mc.gold,
                    modifier = Modifier.width(30.dp).height(30.dp))
                if (recipe.cooks) {
                    Text(cookLabel(recipe), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.dim)
                }
            }
            ItemSlot(recipe.resultItem, cache, size = 56.dp, count = recipe.resultCount)
        }

        Spacer(Modifier.height(10.dp))
        IngredientLegend(recipe, cache, onPickItem)
        if (recipe.source.isNotBlank()) {
            Text(
                "from ${recipe.source}", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 6.dp),
            )
        }
    }
}

@Composable
private fun CraftingGrid(recipe: Recipe, cache: IconCache, onPick: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        for (r in 0 until recipe.gridH) {
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                for (c in 0 until recipe.gridW) {
                    val id = recipe.cell(r, c)
                    ItemSlot(id, cache, size = 38.dp, onClick = if (id.isNotBlank()) ({ onPick(id) }) else null)
                }
            }
        }
    }
}

@Composable
private fun InputRow(recipe: Recipe, cache: IconCache, onPick: (String) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
        recipe.ingredients.forEach { ing ->
            val id = ing.options.firstOrNull().orEmpty()
            ItemSlot(id, cache, size = 46.dp, count = ing.perCraft,
                onClick = if (id.isNotBlank() && !id.startsWith("#")) ({ onPick(id) }) else null)
        }
    }
}

@Composable
private fun IngredientLegend(recipe: Recipe, cache: IconCache, onPick: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        recipe.ingredients.forEach { ing ->
            val id = ing.options.firstOrNull().orEmpty()
            val tag = id.startsWith("#")
            val label = when {
                tag -> "Any " + prettyItem(id.removePrefix("#"))
                else -> cache.nameOf(id) ?: prettyItem(id)
            }
            val row = Modifier.fillMaxWidth().let {
                if (!tag && id.isNotBlank()) it.clickable { onPick(id) } else it
            }
            Row(row.padding(vertical = 1.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("${ing.perCraft}×", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.mc.gold,
                    modifier = Modifier.width(34.dp))
                Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

@Composable
private fun CategoryBadge(category: String, rtype: String) {
    val c = MaterialTheme.mc
    val color = when (category) {
        "crafting" -> c.grassLight
        "smelting", "blasting", "smoking", "campfire" -> c.gold
        "stonecutting" -> c.info
        "smithing" -> c.warning
        else -> c.dim
    }
    Box(Modifier.background(color.copy(alpha = 0.16f)).padding(horizontal = 8.dp, vertical = 3.dp)) {
        Text(rtype.uppercase(), style = MaterialTheme.typography.labelSmall, color = color)
    }
}

private fun cookLabel(recipe: Recipe): String {
    val secs = recipe.cookSeconds
    val s = if (secs == secs.toLong().toDouble()) "${secs.toLong()}s" else "%.1fs".format(secs)
    return if (recipe.experience > 0) "$s · ${"%.1f".format(recipe.experience)}xp" else s
}

internal fun prettyItem(id: String): String =
    id.substringAfter(':')
        .replace('/', ' ').replace('_', ' ')
        .split(' ').filter { it.isNotBlank() }
        .joinToString(" ") { it.replaceFirstChar { ch -> ch.uppercaseChar() } }
