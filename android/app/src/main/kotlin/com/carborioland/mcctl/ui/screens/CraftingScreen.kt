package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.CraftPlan
import com.carborioland.mcctl.core.model.Ingredient
import com.carborioland.mcctl.core.model.Recipe
import com.carborioland.mcctl.core.rpc.RpcException
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.LocalMessenger
import com.carborioland.mcctl.ui.components.AsyncContent
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.EmptyHint
import com.carborioland.mcctl.ui.components.HoldCraftButton
import com.carborioland.mcctl.ui.components.KeyValue
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.McTextField
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.components.UiState
import com.carborioland.mcctl.ui.components.pixelBevel
import com.carborioland.mcctl.ui.rememberActionRunner
import com.carborioland.mcctl.ui.rememberRpcResource
import com.carborioland.mcctl.ui.theme.mc
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The recipe browser + command-craft renderer (TODO Phase 2.5). Search the pack's recipes,
 * open one to plan it against live inventory, and craft it with the pack's signature
 * gesture: **tap** the green button to craft one, **press-and-hold** past the server's
 * `[crafting].hold_ms` to craft the maximum (capped at one output stack). Every craft is
 * gated like any other action (biometric, when the profile asks); `#tag` ingredients can be
 * expanded to the concrete items they accept. The Python core is the brain — this only
 * renders `recipes.*` / `craft.*`.
 */
@Composable
fun CraftingScreen(container: AppContainer) {
    var query by remember { mutableStateOf("") }
    var activeQuery by remember { mutableStateOf("") }
    var selected by remember { mutableStateOf<Recipe?>(null) }

    val res = rememberRpcResource(container, key = activeQuery) { it.recipesSearch(activeQuery, 80) }

    val sel = selected
    if (sel != null) {
        RecipeDetail(container, sel, onBack = { selected = null })
        return
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        McPanel {
            SectionLabel("Recipe browser")
            Row(Modifier.fillMaxWidth().padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                McTextField("Search recipes or items", query, { query = it }, modifier = Modifier.weight(1f))
                McButton("Search", kind = BtnKind.Primary, modifier = Modifier.padding(start = 10.dp), onClick = { activeQuery = query.trim() })
            }
            Text(
                "Read from the pack's mod jars + world datapacks. Tap a recipe to plan and craft it.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 6.dp),
            )
        }

        AsyncContent(res.state, onRetry = res.reload) { search ->
            if (search.recipes.isEmpty()) {
                EmptyHint(if (activeQuery.isBlank()) "No crafting recipes found in the pack." else "No recipes match \"$activeQuery\".")
            } else {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    item {
                        Text(
                            "${search.recipes.size} recipe(s)" + if (search.truncated) " — refine to narrow it down" else "",
                            style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.grassLight,
                            modifier = Modifier.padding(bottom = 2.dp),
                        )
                    }
                    items(search.recipes) { recipe -> RecipeRow(recipe) { selected = recipe } }
                }
            }
        }
    }
}

@Composable
internal fun RecipeRow(r: Recipe, onClick: () -> Unit) {
    McPanel(Modifier.clickable(onClick = onClick)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(prettyItem(r.resultItem), style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(r.id, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Column(horizontalAlignment = Alignment.End) {
                if (r.resultCount > 1) Text("×${r.resultCount}", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.gold)
                Text(r.type.uppercase(), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.info)
            }
        }
    }
}

@Composable
internal fun RecipeDetail(container: AppContainer, recipe: Recipe, onBack: () -> Unit) {
    val runner = rememberActionRunner(container)
    val canAct = Capability.ACTIONS in container.repository.capabilities
    val preview = rememberRpcResource(container, key = recipe.id) { it.craftPreview(recipe.id, count = null) }
    val scope = rememberCoroutineScope()
    val messenger = LocalMessenger.current
    val tagItems = remember { mutableStateMapOf<String, List<String>>() }
    val tagLoading = remember { mutableStateListOf<String>() }
    val pretty = prettyItem(recipe.resultItem)

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            McButton("‹ Back", kind = BtnKind.Neutral, onClick = onBack)
            Spacer(Modifier.width(10.dp))
            Text(pretty, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        }

        McPanel {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Makes", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim)
                Text("${recipe.resultCount}× $pretty", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface)
            }
            KeyValue("Recipe", recipe.id)
            KeyValue("Type", recipe.type)
            if (recipe.source.isNotBlank()) KeyValue("From", recipe.source)
            if (recipe.shaped && recipe.pattern.isNotEmpty()) {
                Spacer(Modifier.height(10.dp))
                PatternGrid(recipe.pattern)
            }
        }

        McPanel {
            SectionLabel("Ingredients")
            val planIngredients = when (val s = preview.state) {
                is UiState.Data -> s.value.ingredients
                else -> null
            }
            (planIngredients ?: recipe.ingredients).forEach { ing ->
                val t = ing.tag
                IngredientRow(
                    ing = ing,
                    resolved = t?.let { tagItems[it] },
                    loading = t != null && t in tagLoading,
                    onExpand = { tag ->
                        if (tag !in tagItems && tag !in tagLoading) {
                            tagLoading += tag
                            scope.launch {
                                try {
                                    val items = withContext(Dispatchers.IO) { container.repository.requireClient().recipesTag(tag).items }
                                    tagItems[tag] = items
                                } catch (e: RpcException) {
                                    messenger(e.friendly())
                                } catch (e: Exception) {
                                    messenger(e.message ?: "error")
                                } finally {
                                    tagLoading -= tag
                                }
                            }
                        }
                    },
                )
            }
        }

        AsyncContent(preview.state, onRetry = preview.reload) { plan ->
            CraftControls(
                plan = plan,
                pretty = pretty,
                canAct = canAct,
                busy = runner.busy,
                onCraft = { count ->
                    runner.run(
                        if (count == null) "Craft max $pretty" else "Craft $pretty",
                        refreshAfter = false,
                        onComplete = preview.reload,
                    ) { c ->
                        val r = c.craftDo(recipe.id, count)
                        when {
                            !r.ok -> "Couldn't craft — ${r.detail.ifBlank { "not enough materials" }}"
                            r.detail.isNotBlank() -> "Crafted ${r.outputCount}× $pretty · ${r.detail}"
                            else -> "Crafted ${r.outputCount}× $pretty"
                        }
                    }
                },
            )
        }
    }
}

@Composable
private fun CraftControls(plan: CraftPlan, pretty: String, canAct: Boolean, busy: Boolean, onCraft: (Int?) -> Unit) {
    McPanel {
        SectionLabel("Craft")
        if (!plan.online) {
            Text(
                "${plan.source} is offline — come online so materials can be read and crafted.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.warning, modifier = Modifier.padding(vertical = 4.dp),
            )
        }
        KeyValue("Craftable now", "${plan.craftable}", valueColor = if (plan.canCraft) MaterialTheme.mc.success else MaterialTheme.mc.dim)
        KeyValue("One-stack cap", "${plan.cap}")
        KeyValue("Materials → output", "${plan.source} → ${plan.receiver}")

        Spacer(Modifier.height(10.dp))
        HoldCraftButton(
            label = "Craft 1",
            holdLabel = "Hold → ${plan.willCraft}×",
            holdMs = plan.holdMs,
            enabled = canAct && plan.canCraft && !busy,
            onTap = { onCraft(1) },
            onHold = { onCraft(null) },
        )
        Text(
            if (!canAct) "Crafting needs the actions capability — enable it in Connection."
            else "Tap = 1 · hold ${holdSecondsLabel(plan.holdMs)} = max (${plan.willCraft}×, one stack). Only loose inventory is consumed — it can't dupe.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim, modifier = Modifier.padding(top = 8.dp),
        )
        if (plan.canCraft && plan.limitedBy == "stack") {
            Text("Hold is capped at one output stack.", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.mc.dim)
        }
    }
}

@Composable
private fun IngredientRow(ing: Ingredient, resolved: List<String>?, loading: Boolean, onExpand: (String) -> Unit) {
    Column(Modifier.fillMaxWidth().padding(vertical = 5.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("${ing.perCraft}×", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.mc.gold, modifier = Modifier.width(42.dp))
            Column(Modifier.weight(1f).padding(end = 8.dp)) {
                Text(ingredientLabel(ing), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface)
                val loose = ing.loose
                if (loose != null) {
                    val extra = ing.stored?.takeIf { it > 0 }?.let { " · +$it in storage" } ?: ""
                    Text(
                        "have $loose$extra",
                        style = MaterialTheme.typography.bodySmall,
                        color = if (loose >= ing.perCraft) MaterialTheme.mc.success else MaterialTheme.mc.danger,
                    )
                }
            }
            ing.tag?.let { tag ->
                if (resolved == null) {
                    McButton(if (loading) "…" else "Items", kind = BtnKind.Neutral, enabled = !loading, onClick = { onExpand(tag) })
                }
            }
        }
        if (resolved != null) {
            Text(
                if (resolved.isEmpty()) "(tag resolves to no items)" else resolved.joinToString(", ") { prettyItem(it) },
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.mc.dim,
                modifier = Modifier.padding(start = 50.dp, top = 2.dp),
            )
        }
    }
}

/** The shaped grid as filled / empty pixel cells (keys are recipe-local, so just the shape). */
@Composable
private fun PatternGrid(pattern: List<String>) {
    val width = (pattern.maxOfOrNull { it.length } ?: 0).coerceAtLeast(1)
    val c = MaterialTheme.mc
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        pattern.forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                for (i in 0 until width) {
                    val filled = (row.getOrNull(i) ?: ' ') != ' '
                    Box(
                        Modifier.size(24.dp).then(
                            if (filled) Modifier.pixelBevel(c.stoneFill, c.stoneLight, c.stoneDark, thickness = 2.dp)
                            else Modifier.background(Color(0xFF15151A)),
                        ),
                    )
                }
            }
        }
    }
}

private fun ingredientLabel(ing: Ingredient): String {
    if (ing.options.isEmpty()) return "?"
    return ing.options.joinToString("  /  ") { opt ->
        if (opt.startsWith("#")) "Any " + prettyItem(opt.removePrefix("#")) else prettyItem(opt)
    }
}

/** "minecraft:oak_planks" -> "Oak Planks"; namespace and separators dropped. */
private fun prettyItem(id: String): String =
    id.substringAfter(':')
        .replace('/', ' ').replace('_', ' ')
        .split(' ').filter { it.isNotBlank() }
        .joinToString(" ") { it.replaceFirstChar { ch -> ch.uppercaseChar() } }

private fun holdSecondsLabel(ms: Int): String {
    val s = ms / 1000.0
    return if (s == s.toLong().toDouble()) "${s.toLong()}s" else "%.1fs".format(s)
}
