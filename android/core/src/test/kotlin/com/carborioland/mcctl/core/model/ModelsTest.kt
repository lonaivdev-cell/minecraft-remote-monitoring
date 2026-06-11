package com.carborioland.mcctl.core.model

import com.carborioland.mcctl.core.rpc.McctlJson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ModelsTest {

    @Test
    fun `metric sample derives heap and mem percentages like the desktop`() {
        val s = MetricSample(
            tps = 19.5, mspt = 12.0, players = 3, load1 = 1.25,
            heapUsed = 6_000_000_000, heapMax = 12_000_000_000,
            memUsed = 8_000_000_000, memTotal = 16_000_000_000,
        )
        assertEquals(50.0, s.value("heap")!!, 0.001)
        assertEquals(50.0, s.value("mem")!!, 0.001)
        assertEquals(19.5, s.value("tps")!!, 0.001)
        assertEquals(12.0, s.value("mspt")!!, 0.001)
        assertEquals(3.0, s.value("players")!!, 0.001)
        assertEquals(1.25, s.value("load")!!, 0.001)
    }

    @Test
    fun `metric sample falls back to committed heap and tolerates missing data`() {
        val s = MetricSample(heapUsed = 3_000_000_000, heapCommitted = 6_000_000_000, heapMax = null)
        assertEquals(50.0, s.value("heap")!!, 0.001)
        assertNull(MetricSample().value("mem"))
        assertNull(MetricSample(memUsed = 1, memTotal = 0).value("mem")) // guards divide-by-zero
    }

    @Test
    fun `status base state mirrors the GUI badge`() {
        assertEquals(ServerState.UNREACHABLE, Status(errors = listOf("ssh failed")).baseState())
        assertEquals(ServerState.ONLINE, Status(running = true, portOpen = true).baseState())
        assertEquals(ServerState.BOOTING, Status(running = true, portOpen = false).baseState())
        assertEquals(ServerState.OFFLINE, Status(running = false).baseState())
    }

    @Test
    fun `metric samples decode straight from agent json with snake_case keys`() {
        val json = """{"ts":1718000000,"running":true,"players":4,"tps":18.7,"mspt":15.0,
            "heap_used":7000000000,"heap_max":12000000000,"mem_used":9000000000,
            "mem_total":16000000000,"load1":2.1,"disk_free":50000000000,"log_age":3}""".trimIndent()
        val s = McctlJson.decodeFromString(MetricSample.serializer(), json)
        assertEquals(4, s.players)
        assertEquals(7_000_000_000, s.heapUsed)
        assertEquals(2.1, s.load1!!, 0.001)
    }
}
