package com.carborioland.mcctl.core.rpc

import com.carborioland.mcctl.core.model.Capability
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentClientTest {

    /** A fixture agent: snake_case results, just like the Python `to_dict()` output. */
    private fun agent(extraCaps: Set<String> = emptySet()) = FakeTransport { method, id, params ->
        when (method) {
            "agent.hello" -> FakeTransport.reply(id, buildJsonObject {
                put("protocol", JsonPrimitive(1))
                put("mcctl_version", JsonPrimitive("0.5.0"))
                put("capabilities", buildJsonArray { params["capabilities"]?.let { add(it) } }.let {
                    // echo back whatever the client requested (that's what the real agent does)
                    params["capabilities"] ?: buildJsonArray { }
                })
                put("methods", buildJsonArray { add(JsonPrimitive("status")) })
            })
            "status" -> FakeTransport.reply(id, statusFixture(fast = params["fast"]?.jsonPrimitive?.content == "true"))
            "start" ->
                if ("actions" in extraCaps) FakeTransport.reply(id, buildJsonObject { put("ok", JsonPrimitive(true)) })
                else FakeTransport.errorReply(id, RpcCodes.CAPABILITY_REQUIRED, "needs the 'actions' capability")
            "kill" -> FakeTransport.reply(id, buildJsonObject { put("ok", JsonPrimitive(true)) })
            "backup.list" -> FakeTransport.reply(id, buildJsonObject {
                put("backups", buildJsonArray {
                    add(buildJsonObject {
                        put("name", JsonPrimitive("world-20260611.tar.zst"))
                        put("path", JsonPrimitive("/opt/minecraft-backups/world-20260611.tar.zst"))
                        put("ts", JsonPrimitive("2026-06-11T04:30:00"))
                        put("size", JsonPrimitive(123456789L))
                        put("full", JsonPrimitive(false))
                        put("age_s", JsonPrimitive(3600.0))
                    })
                })
            })
            // "nobody online" -> the agent returns a JSON null result.
            "players.list" -> buildJsonObject {
                put("jsonrpc", JsonPrimitive("2.0"))
                put("id", JsonPrimitive(id))
                put("result", kotlinx.serialization.json.JsonNull)
            }
            "server.unreachable" -> FakeTransport.errorReply(
                id, RpcCodes.APP, "ssh BatchMode failed",
                buildJsonObject { put("exit_code", JsonPrimitive(3)) }
            )
            else -> FakeTransport.errorReply(id, RpcCodes.METHOD_NOT_FOUND, "unknown method: $method")
        }
    }

    private fun statusFixture(fast: Boolean) = buildJsonObject {
        put("running", JsonPrimitive(true))
        put("pid", JsonPrimitive(4242))
        put("uptime_s", JsonPrimitive(7200))
        put("port_open", JsonPrimitive(true))
        put("tmux_session", JsonPrimitive("minecraft"))
        put("last_backup_age_s", JsonPrimitive(3600))
        put("players", buildJsonObject {
            put("count", JsonPrimitive(2))
            put("max", JsonPrimitive(20))
            put("names", buildJsonArray { add(JsonPrimitive("Steve")); add(JsonPrimitive("Alex")) })
        })
        if (!fast) {
            put("tps", buildJsonObject {
                put("tps", buildJsonObject { put("10s", JsonPrimitive(19.8)); put("1m", JsonPrimitive(20.0)) })
                put("mspt", buildJsonObject { put("median", JsonPrimitive(11.2)) })
            })
        }
    }

    @Test
    fun `hello negotiates capabilities and maps snake_case`() = runTest {
        val t = agent()
        val client = AgentClient(t, backgroundScope)
        client.open()
        val hello = client.hello(setOf(Capability.ACTIONS))
        assertEquals(1, hello.protocol)
        assertEquals("0.5.0", hello.mcctlVersion)
        assertTrue(Capability.ACTIONS in client.capabilities)
    }

    @Test
    fun `status decodes snake_case fields and helper readings`() = runTest {
        val client = AgentClient(agent(), backgroundScope).also { it.open() }
        val st = client.status(fast = false)
        assertTrue(st.running)
        assertEquals(4242, st.pid)
        assertEquals(7200, st.uptimeS)
        assertTrue(st.portOpen)
        assertEquals("minecraft", st.tmuxSession)
        assertEquals(2, st.players?.count)
        assertEquals(19.8, st.tpsNow()!!, 0.001)
        assertEquals(11.2, st.msptMedian()!!, 0.001)
        assertEquals(com.carborioland.mcctl.core.model.ServerState.ONLINE, st.baseState())
    }

    @Test
    fun `capability-gated method surfaces a typed error when not negotiated`() = runTest {
        val client = AgentClient(agent(extraCaps = emptySet()), backgroundScope).also { it.open() }
        val ex = runCatching { client.start() }.exceptionOrNull()
        assertTrue(ex is RpcException)
        assertTrue((ex as RpcException).needsCapability)
    }

    @Test
    fun `capability granted lets the action through`() = runTest {
        val client = AgentClient(agent(extraCaps = setOf("actions")), backgroundScope).also { it.open() }
        assertTrue(client.start())
    }

    @Test
    fun `destructive methods always send confirm true`() = runTest {
        val t = agent()
        val client = AgentClient(t, backgroundScope).also { it.open() }
        client.kill()
        val killReq = t.sent.first { it.contains("\"kill\"") }
        val params = McctlJson.parseToJsonElement(killReq).jsonObject["params"]!!.jsonObject
        assertEquals(true, params["confirm"]?.jsonPrimitive?.content?.toBoolean())
    }

    @Test
    fun `server-unreachable error carries exit code 3`() = runTest {
        val client = AgentClient(agent(), backgroundScope).also { it.open() }
        val ex = runCatching { client.callRaw("server.unreachable") }.exceptionOrNull() as RpcException
        assertTrue(ex.serverUnreachable)
        assertEquals(3, ex.exitCode)
    }

    @Test
    fun `backup list parses age and size`() = runTest {
        val client = AgentClient(agent(), backgroundScope).also { it.open() }
        val backups = client.backupList()
        assertEquals(1, backups.size)
        assertEquals("world-20260611.tar.zst", backups[0].name)
        assertEquals(123456789L, backups[0].size)
        assertFalse(backups[0].full)
    }

    @Test
    fun `players list tolerates a null result (nobody online)`() = runTest {
        val client = AgentClient(agent(), backgroundScope).also { it.open() }
        assertNull(client.playersList())
    }

    // Unconfined so the reader and the collector run eagerly: the event is delivered the
    // instant it is pushed, with no subscribe-before-emit race to schedule around.
    @Test
    fun `event notifications fan out on the events flow`() = runTest(UnconfinedTestDispatcher()) {
        val t = agent()
        val client = AgentClient(t, backgroundScope).also { it.open() }
        val collected = mutableListOf<com.carborioland.mcctl.core.model.WatchdogEvent>()
        val job = backgroundScope.launch { client.events.collect { collected += it } }
        t.pushEvent(buildJsonObject {
            put("ts", JsonPrimitive(1.0))
            put("kind", JsonPrimitive("alert-tps"))
            put("detail", JsonPrimitive("TPS 11.4"))
            put("urgency", JsonPrimitive("critical"))
        })
        assertEquals(1, collected.size)
        assertEquals("alert-tps", collected.single().kind)
        assertTrue(collected.single().critical)
        job.cancel()
    }
}
