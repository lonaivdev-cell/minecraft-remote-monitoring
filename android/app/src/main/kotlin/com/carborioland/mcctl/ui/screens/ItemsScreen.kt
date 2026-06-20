package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.ItemEntry
import com.carborioland.mcctl.core.model.Recipe
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.ItemIcon
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc

/**
 * The EMI-style item browser (TODO Phase 2.6): every item the pack defines, drawn with its real
 * icon and searchable by name or id. Tap an item to see the recipes that make it. The icons come
 * from the brain's `items.manifest` (id → name → texture) + `icons.fetch` (PNG bytes), cached by
 * [com.carborioland.mcctl.ui.IconCache]; "Get vanilla icons" runs `assets.sync` so base-game items
 * gain icons too. This only renders — the Python core resolves and de-duplicates the assets.
 */
@Composable
fun ItemsScreen(container: AppContainer) {
    var query by remember { mutableStateOf("") }
    var activeQuery by remember { mutableStateOf("") }
    var picked by remember { mutableStateOf<ItemEntry?>(null) }

    val cache = container.iconCache
    val res = rememberRpcResource(container, key = activeQuery) { it.itemsManifest(activeQuery, limit = 400) }

    val sel = picked
    if (sel != null) {
        ItemRecipes(container, sel, onBack = { picked = null })
        return
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            SectionLabel("Item browser")
            Row(Modifier.fillMaxWidth().padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                McTextField("Search items by name or id", query, { query = it }, modifier = Modifier.weight(1f))
                McButton("Search", kind = BtnKind.Primary, modifier = Modifier.padding(start = 10.dp), onClick = { activeQuery = query.trim() })
            }
            VanillaIconsButton(container, onSynced = { cache.clear(); res.reload() })
        }

        AsyncContent(res.state, onRetry = res.reload) { manifest ->
            if (manifest.items.isEmpty()) {
                EmptyHint(
                    if (activeQuery.isBlank())
                        "No items found. Tap \"Get vanilla icons\" to add the base game, or drop a resource pack in resourcepacks/."
                    else "No items match \"$activeQuery\".",
                )
            } else {
                LaunchedEffect(manifest) {
                    manifest.items.asSequence().map { it.icon }.filter { it.isNotBlank() }.distinct()
                        .toList().chunked(200).forEach { cache.ensure(it) }
                }
                Column(Modifier.fillMaxSize()) {
                    Text(
                        "${manifest.count} item(s)" + if (manifest.truncated) " — refine to narrow it down" else "",
                        style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight,
                        modifier = Modifier.padding(bottom = 6.dp),
                    )
                    LazyVerticalGrid(
                        columns = GridCells.Adaptive(minSize = 88.dp),
                        modifier = Modifier.fillMaxSize(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        items(manifest.items, key = { it.id }) { entry -> ItemCell(entry, cache) { picked = entry } }
                    }
                }
            }
        }
    }
}

@Composable
private fun ItemCell(entry: ItemEntry, cache: com.carborioland.mcctl.ui.IconCache, onClick: () -> Unit) {
    McPanel(Modifier.clickable(onClick = onClick)) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            ItemIcon(entry.icon, cache, size = 40.dp, fallbackLabel = entry.name)
            Spacer(Modifier.height(6.dp))
            Text(
                entry.name, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface,
                maxLines = 2, overflow = TextOverflow.Ellipsis, textAlign = TextAlign.Center,
            )
        }
    }
}

/** Tap-through from an item to the recipes that produce it, then into the full craft view. */
@Composable
private fun ItemRecipes(container: AppContainer, entry: ItemEntry, onBack: () -> Unit) {
    var openRecipe by remember { mutableStateOf<Recipe?>(null) }

    val open = openRecipe
    if (open != null) {
        RecipeDetail(container, open, onBack = { openRecipe = null })
        return
    }

    val res = rememberRpcResource(container, key = entry.id) { it.recipesSearch(entry.id, 80) }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            McButton("‹ Back", kind = BtnKind.Neutral, onClick = onBack)
            Spacer(Modifier.width(10.dp))
            ItemIcon(entry.icon, container.iconCache, size = 28.dp, fallbackLabel = entry.name)
            Spacer(Modifier.width(8.dp))
            Text(
                entry.name, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
            )
        }
        AsyncContent(res.state, onRetry = res.reload) { search ->
            val makes = search.recipes.filter { it.resultItem == entry.id }
            if (makes.isEmpty()) {
                EmptyHint("No recipe makes ${entry.name}. It may be a base resource, a mob drop, or made in a machine the browser doesn't cover yet.")
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        "${makes.size} recipe(s) make this", style = MaterialTheme.typography.titleSmall,
                        color = MaterialTheme.mc.grassLight,
                    )
                    makes.forEach { r -> RecipeRow(r) { openRecipe = r } }
                }
            }
        }
    }
}

@Composable
private fun VanillaIconsButton(container: AppContainer, onSynced: () -> Unit) {
    val runner = rememberActionRunner(container)
    val canAct = Capability.ACTIONS in container.repository.capabilities
    if (!canAct) {
        Text(
            "Base-game items need the vanilla icons. Enable the actions capability in Connection to fetch them.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 8.dp),
        )
        return
    }
    Column(Modifier.padding(top = 8.dp)) {
        McButton(
            "Get vanilla icons", kind = BtnKind.Gold, enabled = !runner.busy,
            onClick = {
                runner.run("Fetch the vanilla client jar", refreshAfter = false, onComplete = onSynced) { client ->
                    val r = client.assetsSync()
                    if (r.ok) "Vanilla ${r.version} ready (${r.status})" else "Couldn't fetch vanilla assets (${r.status})"
                }
            },
        )
        Text(
            "One-time ~25 MB download on the server so base-game items get icons + names.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 6.dp),
        )
    }
}
