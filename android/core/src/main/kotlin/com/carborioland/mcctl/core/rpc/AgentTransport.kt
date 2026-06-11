package com.carborioland.mcctl.core.rpc

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * A bidirectional, line-oriented byte pipe to a running `mcctl agent`. The SSH channel
 * is one such pipe; [FakeTransport] is another for tests. The framing (one compact JSON
 * object per `\n`-terminated line) lives in [AgentClient], not here.
 */
interface AgentTransport {
    /** Inbound NDJSON lines from the agent. Cold; collected once by the client. */
    fun receiveLines(): Flow<String>

    /** Send one NDJSON line (the client guarantees it has no embedded newline). */
    suspend fun send(line: String)

    /** Tear down the channel; the agent exits on EOF. */
    suspend fun close()
}

/**
 * An in-memory transport that answers requests from a [responder] — the "recorded RPC
 * fixture server" the Android testing strategy calls for. It can also push unsolicited
 * `event` notifications to exercise the event stream.
 *
 * The responder receives the method, request id, and params, and returns the *full*
 * response envelope (use [reply] / [errorReply] to build one).
 */
class FakeTransport(
    private val responder: (method: String, id: Int, params: JsonObject) -> JsonObject,
) : AgentTransport {

    private val channel = Channel<String>(Channel.UNLIMITED)
    val sent = mutableListOf<String>()

    override fun receiveLines(): Flow<String> = channel.receiveAsFlow()

    override suspend fun send(line: String) {
        sent += line
        val obj = McctlJson.parseToJsonElement(line).jsonObject
        val id = obj["id"]!!.jsonPrimitive.int
        val method = obj["method"]!!.jsonPrimitive.content
        val params = obj["params"] as? JsonObject ?: JsonObject(emptyMap())
        val envelope = responder(method, id, params)
        channel.send(McctlJson.encodeToString(JsonObject.serializer(), envelope))
    }

    /** Push a server-initiated `event` notification. */
    suspend fun pushEvent(params: JsonObject) {
        val envelope = buildJsonObject {
            put("jsonrpc", JsonPrimitive("2.0"))
            put("method", JsonPrimitive("event"))
            put("params", params)
        }
        channel.send(McctlJson.encodeToString(JsonObject.serializer(), envelope))
    }

    override suspend fun close() {
        channel.close()
    }

    companion object {
        /** Build a successful response envelope wrapping [result]. */
        fun reply(id: Int, result: JsonObject): JsonObject = buildJsonObject {
            put("jsonrpc", JsonPrimitive("2.0"))
            put("id", JsonPrimitive(id))
            put("result", result)
        }

        /** Build an error response envelope. */
        fun errorReply(id: Int, code: Int, message: String, data: JsonObject? = null): JsonObject =
            buildJsonObject {
                put("jsonrpc", JsonPrimitive("2.0"))
                put("id", JsonPrimitive(id))
                put("error", buildJsonObject {
                    put("code", JsonPrimitive(code))
                    put("message", JsonPrimitive(message))
                    if (data != null) put("data", data)
                })
            }
    }
}
