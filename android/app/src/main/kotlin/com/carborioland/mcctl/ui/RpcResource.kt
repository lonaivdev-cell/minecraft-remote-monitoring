package com.carborioland.mcctl.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.carborioland.mcctl.core.rpc.AgentClient
import com.carborioland.mcctl.core.rpc.RpcException
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.ui.components.UiState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** A loaded RPC value plus a [reload] trigger. */
class RpcResource<T>(val state: UiState<T>, val reload: () -> Unit)

/**
 * Loads a value from the agent into a [UiState] and re-runs on [reload] (or when [key]
 * changes). RPC errors render with their friendly hint. The work runs on IO; the screen
 * just renders the resulting state. This is the backbone of every read-mostly screen.
 */
@Composable
fun <T> rememberRpcResource(
    container: AppContainer,
    key: Any = Unit,
    load: suspend (AgentClient) -> T,
): RpcResource<T> {
    var state by remember(key) { mutableStateOf<UiState<T>>(UiState.Loading) }
    var tick by remember(key) { mutableIntStateOf(0) }

    LaunchedEffect(key, tick) {
        state = UiState.Loading
        state = try {
            val value = withContext(Dispatchers.IO) { load(container.repository.requireClient()) }
            UiState.Data(value)
        } catch (e: CancellationException) {
            // The screen left composition (or key/tick changed): let cancellation
            // propagate instead of rendering "Job was cancelled" as a fake error banner.
            throw e
        } catch (e: RpcException) {
            UiState.Error(e.friendly())
        } catch (e: Exception) {
            UiState.Error(e.message ?: "error")
        }
    }
    return RpcResource(state) { tick++ }
}
