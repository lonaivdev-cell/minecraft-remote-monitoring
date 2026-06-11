package com.carborioland.mcctl.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.components.BtnKind
import com.carborioland.mcctl.ui.components.McButton
import com.carborioland.mcctl.ui.components.McPanel
import com.carborioland.mcctl.ui.components.SectionLabel
import com.carborioland.mcctl.ui.theme.mc

/**
 * Placeholder for the AI analysis/chat features (`mcctl ai`). Wired intentionally as a
 * stub: running an LLM — cloud or on-device — is out of scope for this first app cut, so
 * the screen documents what it *will* do rather than pretending to work.
 */
@Composable
fun AiScreen() {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Box(Modifier.fillMaxSize().padding(top = 24.dp), contentAlignment = Alignment.TopCenter) {
            Icon(Icons.Filled.AutoAwesome, contentDescription = null, tint = MaterialTheme.mc.info, modifier = Modifier.size(56.dp))
        }
        McPanel {
            SectionLabel("AI analysis — coming soon")
            Text(
                "On the desktop, mcctl can review logs, root-cause crash reports, explain the mods " +
                    "and answer free-form questions — powered by Claude or a local ollama model, with " +
                    "live server context attached and crash-log prompt-injection sealed off.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.mc.dim,
            )
            Text(
                "That isn't in this first build of the phone app: a cloud key on a phone and an " +
                    "on-device model are both their own project. The agent already carries everything " +
                    "the analysis needs (logs, crash reports, mods, inspect), so the wiring is the only " +
                    "thing left for a later cycle.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.mc.dim,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
        McPanel {
            SectionLabel("Planned")
            listOf(
                "Review logs — summarize what's happening right now",
                "Analyze latest crash — exception, suspect mod, fix",
                "Explain the mods — what each one adds",
                "Ask a question — with live status & log attached",
            ).forEach { Text("• $it", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.padding(vertical = 3.dp)) }
            McButton("Analyze (disabled)", onClick = {}, kind = BtnKind.Neutral, enabled = false, modifier = Modifier.padding(top = 12.dp))
        }
        Text(
            "Until then, use the Crashes screen's Postmortem — a deterministic root-cause read with " +
                "no AI required.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.mc.dim,
        )
    }
}
