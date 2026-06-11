package com.carborioland.mcctl.core.ssh

import com.carborioland.mcctl.core.rpc.AgentTransport
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import net.schmizz.sshj.DefaultConfig
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.connection.channel.direct.Session
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream

/** Where to reach the agent and how to launch it. */
data class SshTarget(
    val host: String,
    val port: Int = 22,
    val user: String,
    val agentCommand: String = "mcctl agent",
    val connectTimeoutMs: Int = 15_000,
    val keepAliveSeconds: Int = 15,
)

/**
 * The production [AgentTransport]: one SSH channel running `mcctl agent`. Its stdin is
 * the request pipe, its stdout the NDJSON reply/event pipe. No port is opened and no
 * password is stored — auth is the device's Ed25519 key, exactly as the security model
 * requires (the agent runs as the same unprivileged user on the box).
 *
 * Blocking socket I/O is confined to [ioDispatcher]; the reader loop turns the remote
 * stdout into a cold [Flow] the [com.carborioland.mcctl.core.rpc.AgentClient] collects.
 */
class SshAgentTransport(
    private val target: SshTarget,
    private val identity: Ed25519Identity,
    private val verifier: HostKeyVerifier,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) : AgentTransport {

    private var ssh: SSHClient? = null
    private var session: Session? = null
    private var command: Session.Command? = null
    private var writer: OutputStream? = null

    /** Connect, authenticate, and launch the agent. Throws on any failure. */
    suspend fun connect(): Unit = withContext(ioDispatcher) {
        val client = SSHClient(DefaultConfig())
        client.addHostKeyVerifier(verifier)
        client.connectTimeout = target.connectTimeoutMs
        client.timeout = target.connectTimeoutMs
        client.connect(target.host, target.port)
        client.authPublickey(target.user, identity.keyProvider())
        client.connection.keepAlive.keepAliveInterval = target.keepAliveSeconds
        val sess = client.startSession()
        val cmd = sess.exec(target.agentCommand)
        ssh = client
        session = sess
        command = cmd
        writer = cmd.outputStream
    }

    override fun receiveLines(): Flow<String> = flow {
        val input = command?.inputStream ?: error("transport not connected")
        val reader = BufferedReader(InputStreamReader(input, Charsets.UTF_8))
        try {
            while (true) {
                val line = reader.readLine() ?: break
                emit(line)
            }
        } finally {
            runCatching { reader.close() }
        }
    }.flowOn(ioDispatcher)

    override suspend fun send(line: String): Unit = withContext(ioDispatcher) {
        val w = writer ?: error("transport not connected")
        w.write((line + "\n").toByteArray(Charsets.UTF_8))
        w.flush()
    }

    override suspend fun close(): Unit = withContext(ioDispatcher) {
        runCatching { command?.close() }
        runCatching { session?.close() }
        runCatching { ssh?.disconnect() }
        ssh = null
        session = null
        command = null
        writer = null
    }
}
