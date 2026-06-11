package com.carborioland.mcctl.core.rpc

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * The shared JSON codec. camelCase model fields map to the agent's snake_case wire
 * keys; unknown keys are ignored so a newer server can add fields without breaking an
 * older app (the contract's additive-within-a-major-version rule).
 */
@OptIn(ExperimentalSerializationApi::class)
val McctlJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    isLenient = true
    encodeDefaults = false
    namingStrategy = JsonNamingStrategy.SnakeCase
}

/** JSON-RPC 2.0 + mcctl app-level error codes (see `agent.py`). */
object RpcCodes {
    const val PARSE = -32700
    const val INVALID_REQUEST = -32600
    const val METHOD_NOT_FOUND = -32601
    const val INVALID_PARAMS = -32602
    const val INTERNAL = -32603
    const val APP = -32000
    const val CAPABILITY_REQUIRED = -32004
    const val CONFIRM_REQUIRED = -32005
}

/**
 * An error returned by the agent. [exitCode] carries mcctl's CLI exit vocabulary when
 * present (1 generic, 3 server-unreachable), so the UI can distinguish "the box is
 * down" from "the command failed".
 */
class RpcException(
    val code: Int,
    override val message: String,
    val data: JsonObject? = null,
) : Exception(message) {

    val exitCode: Int? get() = data?.get("exit_code")?.jsonPrimitive?.intOrNull

    val needsCapability: Boolean get() = code == RpcCodes.CAPABILITY_REQUIRED
    val needsConfirm: Boolean get() = code == RpcCodes.CONFIRM_REQUIRED
    val serverUnreachable: Boolean get() = exitCode == 3

    /** A short, human-readable line for a toast or banner. */
    fun friendly(): String = when {
        serverUnreachable -> "Server unreachable — $message"
        needsConfirm -> "This action needs confirmation"
        needsCapability -> "This action needs a capability the session didn't request"
        else -> message
    }
}

/** Build one compact NDJSON request line. Never contains a literal newline. */
fun buildRequestLine(id: Int, method: String, params: JsonObject?): String {
    val obj = buildJsonObject {
        put("jsonrpc", JsonPrimitive("2.0"))
        put("id", JsonPrimitive(id))
        put("method", JsonPrimitive(method))
        if (params != null) put("params", params)
    }
    return McctlJson.encodeToString(JsonObject.serializer(), obj)
}

/** A parsed inbound message: either a correlated reply or a server-initiated event. */
sealed interface Inbound {
    data class Reply(val id: Int, val result: JsonObject?, val error: RpcException?) : Inbound
    data class Event(val params: JsonObject) : Inbound
    data class Unparseable(val raw: String) : Inbound
}

/** Parse one NDJSON line from the agent into an [Inbound]. */
fun parseInbound(line: String): Inbound {
    val obj = runCatching { McctlJson.parseToJsonElement(line).jsonObject }.getOrNull()
        ?: return Inbound.Unparseable(line)

    // Server-initiated notification: {"method":"event","params":{...}}, no id.
    if (obj["method"]?.jsonPrimitive?.contentOrNullSafe() == "event") {
        val params = obj["params"] as? JsonObject ?: JsonObject(emptyMap())
        return Inbound.Event(params)
    }

    val id = obj["id"]?.jsonPrimitive?.intOrNull ?: return Inbound.Unparseable(line)
    obj["error"]?.let { err ->
        val e = err.jsonObject
        val code = e["code"]?.jsonPrimitive?.int ?: RpcCodes.INTERNAL
        val msg = e["message"]?.jsonPrimitive?.contentOrNullSafe() ?: "error"
        val data = e["data"] as? JsonObject
        return Inbound.Reply(id, null, RpcException(code, msg, data))
    }
    val result = obj["result"] as? JsonObject
    return Inbound.Reply(id, result, null)
}

private fun JsonPrimitive.contentOrNullSafe(): String? = if (isString) content else null
