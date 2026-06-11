package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.theme.mc

/** A simple async screen state: loading, an error message, or loaded data. */
sealed interface UiState<out T> {
    data object Loading : UiState<Nothing>
    data class Error(val message: String) : UiState<Nothing>
    data class Data<T>(val value: T) : UiState<T>
}

/** Renders the right thing for a [UiState], with a themed loader and error panel. */
@Composable
fun <T> AsyncContent(
    state: UiState<T>,
    modifier: Modifier = Modifier,
    onRetry: (() -> Unit)? = null,
    content: @Composable (T) -> Unit,
) {
    when (state) {
        is UiState.Loading -> Loading(modifier)
        is UiState.Error -> ErrorPanel(state.message, onRetry, modifier)
        is UiState.Data -> content(state.value)
    }
}

@Composable
fun Loading(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
            CircularProgressIndicator(color = MaterialTheme.mc.grassFill, modifier = Modifier.size(40.dp), strokeWidth = 5.dp)
            Text("LOADING…", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.mc.dim)
        }
    }
}

@Composable
fun ErrorPanel(message: String, onRetry: (() -> Unit)? = null, modifier: Modifier = Modifier) {
    McPanel(modifier) {
        Text("Couldn't load that", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.mc.danger)
        Text(message, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim, textAlign = TextAlign.Start)
        if (onRetry != null) {
            McButton("Retry", onRetry, modifier = Modifier.padding(top = 10.dp), kind = BtnKind.Neutral)
        }
    }
}

/** Centered empty-state hint inside a panel. */
@Composable
fun EmptyHint(text: String, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
        Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim, textAlign = TextAlign.Center)
    }
}
