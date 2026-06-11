package com.carborioland.mcctl.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import com.carborioland.mcctl.core.rpc.AgentClient
import com.carborioland.mcctl.core.rpc.RpcException
import com.carborioland.mcctl.data.ServerRepository
import com.carborioland.mcctl.di.AppContainer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Runs a state-changing agent call with the whole ceremony in one place: biometric gate,
 * a single-flight busy flag, error → snackbar (RPC errors render with their friendly
 * hint), and a status refresh afterwards. Shared by every action screen so the safety
 * semantics are identical everywhere.
 */
class ActionRunner(
    private val scope: CoroutineScope,
    private val repo: ServerRepository,
    private val gate: suspend (String) -> Boolean,
    private val messenger: (String) -> Unit,
) {
    var busy by mutableStateOf(false)
        private set

    /**
     * @param reason shown in the biometric prompt and as the "busy" intent.
     * @param confirmed pass true to skip the biometric gate (the caller already showed a
     *   typed confirm dialog for a destructive action).
     * @param work the suspend call; its returned string (if any) becomes a snackbar.
     */
    fun run(
        reason: String,
        confirmed: Boolean = false,
        refreshAfter: Boolean = true,
        onComplete: (() -> Unit)? = null,
        work: suspend (AgentClient) -> String?,
    ) {
        if (busy) {
            messenger("Busy — one action at a time")
            return
        }
        scope.launch {
            if (!confirmed && !gate(reason)) return@launch
            busy = true
            try {
                val msg = withContext(Dispatchers.IO) { work(repo.requireClient()) }
                if (!msg.isNullOrBlank()) messenger(msg)
                if (refreshAfter) repo.refresh(false)
            } catch (e: RpcException) {
                messenger(e.friendly())
            } catch (e: Exception) {
                messenger(e.message ?: "error")
            } finally {
                busy = false
                onComplete?.invoke()
            }
        }
    }
}

@Composable
fun rememberActionRunner(container: AppContainer): ActionRunner {
    val scope = rememberCoroutineScope()
    val gate = LocalActionGate.current
    val messenger = LocalMessenger.current
    return remember { ActionRunner(scope, container.repository, gate, messenger) }
}
