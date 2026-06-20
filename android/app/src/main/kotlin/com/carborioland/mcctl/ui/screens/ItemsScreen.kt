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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items as gridItems
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
import com.carborioland.mcctl.ui.IconCache
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.ErrorPanel
import com.carborioland.mcctl.ui.components.ItemIcon
import com.carborioland.mcctl.ui.components.ItemSlot
import com.carborioland.mcctl.ui.components.Loading
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.RecipeView
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.components.prettyItem
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc

/**
 * The EMI-style item browser (TODO Phase 2.6): every item the pack defines, drawn with its real
 * icon and searchable by name or id. Tap an item to open its detail — the recipes that **make** it
 * and the recipes that **use** it, each rendered as a true EMI card (positional grid, furnace line,
 * counts) — and tap any ingredient to pivot to *that* item. Icons + names come from the brain's
 * `items.manifest`/`icons.fetch` (cached on disk by [IconCache]); "Get vanilla icons" runs
 * `assets.sync`. This only renders; the Python core resolves and de-duplicates the assets.
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
        ItemDetail(container, sel, onBack = { picked = null }, onPick = { id -> picked = entryFor(cache, id) })
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
                        gridItems(manifest.items, key = { it.id }) { entry -> ItemCell(entry, cache) { picked = entry } }
                    }
                }
            }
        }
    }
}

@Composable
private fun ItemCell(entry: ItemEntry, cache: IconCache, onClick: () -> Unit) {
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

/** An item's detail: header, a Recipes/Uses toggle, and EMI recipe cards you can pivot through. */
@Composable
private fun ItemDetail(container: AppContainer, entry: ItemEntry, onBack: () -> Unit, onPick: (String) -> Unit) {
    var tab by remember(entry.id) { mutableStateOf(0) }       // 0 = Recipes (makes), 1 = Uses
    val cache = container.iconCache

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                McButton("‹ Back", kind = BtnKind.Neutral, onClick = onBack)
                Spacer(Modifier.width(10.dp))
                ItemSlot(entry.id, cache, size = 44.dp)
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text(entry.name, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(entry.id, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                McButton("Recipes", kind = if (tab == 0) BtnKind.Primary else BtnKind.Neutral, modifier = Modifier.weight(1f), onClick = { tab = 0 })
                McButton("Uses", kind = if (tab == 1) BtnKind.Primary else BtnKind.Neutral, modifier = Modifier.weight(1f), onClick = { tab = 1 })
            }
        }
        if (tab == 0) MakesList(container, entry, onPick) else UsesList(container, entry, onPick)
    }
}

@Composable
private fun MakesList(container: AppContainer, entry: ItemEntry, onPick: (String) -> Unit) {
    val res = rememberRpcResource(container, key = entry.id) { it.recipesSearch(entry.id, 100) }
    AsyncContent(res.state, onRetry = res.reload) { search ->
        val makes = search.recipes.filter { it.resultItem == entry.id }
        if (makes.isEmpty()) {
            EmptyHint("No recipe makes ${entry.name}. It may be a base resource, a mob drop, or made in a machine the browser doesn't cover yet.")
        } else {
            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(makes) { r -> RecipeView(r, container.iconCache, onPickItem = onPick) }
            }
        }
    }
}

@Composable
private fun UsesList(container: AppContainer, entry: ItemEntry, onPick: (String) -> Unit) {
    var state by remember(entry.id) { mutableStateOf<UsesState>(UsesState.Loading) }
    LaunchedEffect(entry.id) {
        state = UsesState.Loading
        state = try {
            UsesState.Ready(container.recipeStore.ensureLoaded().uses(entry.id))
        } catch (e: Exception) {
            UsesState.Failed(e.message ?: "couldn't load the recipe set")
        }
    }
    when (val s = state) {
        UsesState.Loading -> Loading()
        is UsesState.Failed -> ErrorPanel(s.message)
        is UsesState.Ready ->
            if (s.recipes.isEmpty()) {
                EmptyHint("Nothing in the loaded recipe set uses ${entry.name}.")
            } else {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    items(s.recipes) { r -> RecipeView(r, container.iconCache, onPickItem = onPick) }
                }
            }
    }
}

private sealed interface UsesState {
    data object Loading : UsesState
    data class Failed(val message: String) : UsesState
    data class Ready(val recipes: List<Recipe>) : UsesState
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

private fun entryFor(cache: IconCache, id: String): ItemEntry =
    ItemEntry(id = id, name = cache.nameOf(id) ?: prettyItem(id), icon = cache.textureOf(id) ?: "")
