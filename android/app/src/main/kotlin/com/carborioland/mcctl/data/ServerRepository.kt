package com.carborioland.mcctl.data

import com.carborioland.mcctl.core.model.Capability
import com.carborioland.mcctl.core.model.HelloResult
import com.carborioland.mcctl.core.model.Status
import com.carborioland.mcctl.core.model.WatchdogEvent
import com.carborioland.mcctl.core.rpc.AgentClient
import com.carborioland.mcctl.core.ssh.Ed25519Identity
import com.carborioland.mcctl.core.ssh.GatedHostKeyVerifier
import com.carborioland.mcctl.core.ssh.HostKeyGate
import com.carborioland.mcctl.core.ssh.HostKeyStatus
import com.carborioland.mcctl.core.ssh.SshAgentTransport
import com.carborioland.mcctl.core.ssh.SshTarget
import com.carborioland.mcctl.data.security.SecureStore
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext

/** Connection lifecycle, surfaced to the UI as a single observable state. */
sealed interface ConnState {
    data object Disconnected : ConnState
    data object Connecting : ConnState
    data class Connected(val hello: HelloResult) : ConnState
    data class Failed(val message: String) : ConnState
}

/** A pending host-key decision the UI must resolve (trust-on-first-use / changed key). */
data class HostKeyPrompt(
    val host: String,
    val port: Int,
    val keyType: String,
    val fingerprint: String,
    val status: HostKeyStatus,
    val decision: CompletableDeferred<Boolean>,
)

/**
 * The one object the UI talks to. It owns the SSH transport + [AgentClient], negotiates
 * capabilities from the saved profile, and republishes status and watchdog events as
 * flows the screens observe. There is never more than one live connection.
 */
class ServerRepository(
    private val scope: CoroutineScope,
    private val secure: SecureStore,
) {
    private val _state = MutableStateFlow<ConnState>(ConnState.Disconnected)
    val state: StateFlow<ConnState> = _state.asStateFlow()

    private val _status = MutableStateFlow<Status?>(null)
    val status: StateFlow<Status?> = _status.asStateFlow()

    private val _events = MutableSharedFlow<WatchdogEvent>(replay = 0, extraBufferCapacity = 128)
    val events: SharedFlow<WatchdogEvent> = _events.asSharedFlow()

    private val _hostKeyPrompt = MutableStateFlow<HostKeyPrompt?>(null)
    val hostKeyPrompt: StateFlow<HostKeyPrompt?> = _hostKeyPrompt.asStateFlow()

    private var transport: SshAgentTransport? = null
    var client: AgentClient? = null
        private set

    val identity: Ed25519Identity get() = secure.identity()

    /** The user's verdict on a [HostKeyPrompt]. */
    fun resolveHostKey(accept: Boolean) {
        _hostKeyPrompt.value?.decision?.complete(accept)
    }

    private val gate = HostKeyGate { host, port, keyType, fp, status ->
        val deferred = CompletableDeferred<Boolean>()
        _hostKeyPrompt.value = HostKeyPrompt(host, port, keyType, fp, status, deferred)
        // We are on the SSH I/O thread here; block it until the user taps accept/reject.
        val ok = runBlocking { deferred.await() }
        _hostKeyPrompt.value = null
        ok
    }

    suspend fun connect(profile: ConnectionProfile): Result<HelloResult> {
        disconnect()
        _state.value = ConnState.Connecting
        return runCatching {
            val caps = buildSet {
                if (profile.enableActions) add(Capability.ACTIONS)
                if (profile.enableDestructive) add(Capability.DESTRUCTIVE)
            }
            val verifier = GatedHostKeyVerifier(secure, gate)
            val t = SshAgentTransport(
                target = SshTarget(profile.host, profile.port, profile.user, profile.agentCommand),
                identity = secure.identity(),
                verifier = verifier,
            )
            t.connect()
            val c = AgentClient(t, scope)
            c.open()
            val hello = c.hello(caps)
            // Fan watchdog events from this client into the repo-level flow.
            scope.launch { c.events.collect { _events.emit(it) } }
            transport = t
            client = c
            _state.value = ConnState.Connected(hello)
            refresh(fast = false)
            hello
        }.onFailure { e ->
            _state.value = ConnState.Failed(e.message ?: e.javaClass.simpleName)
            runCatching { transport?.close() }
            transport = null
            client = null
        }
    }

    suspend fun disconnect() {
        runCatching { client?.shutdownAndClose() }
        client = null
        transport = null
        _status.value = null
        _state.value = ConnState.Disconnected
    }

    /** Re-probe status into [status]. Cheap with [fast]=true (skips spark TPS/players/heap). */
    suspend fun refresh(fast: Boolean = false) {
        val c = client ?: return
        withContext(Dispatchers.IO) {
            _status.value = c.status(fast = fast)
        }
    }

    fun requireClient(): AgentClient = client ?: error("not connected")

    val capabilities: Set<Capability> get() = client?.capabilities ?: emptySet()
}
