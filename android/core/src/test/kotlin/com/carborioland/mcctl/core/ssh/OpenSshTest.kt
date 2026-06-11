package com.carborioland.mcctl.core.ssh

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.DataInputStream
import java.util.Base64

class OpenSshTest {

    private val seed = ByteArray(32) { it.toByte() } // deterministic 00,01,02,…,1f

    @Test
    fun `public key line has the canonical ssh-ed25519 shape`() {
        val id = Ed25519Identity.fromSeed(seed)
        val line = id.openSshPublicKey("phone@carborio")
        val parts = line.split(" ")
        assertEquals(3, parts.size)
        assertEquals("ssh-ed25519", parts[0])
        assertEquals("phone@carborio", parts[2])

        // Every ssh-ed25519 public key's base64 begins with this fixed prefix: it encodes
        // the length-prefixed type string "ssh-ed25519" + the 32-byte length header. This
        // is an OpenSSH-defined invariant, independent of our implementation.
        assertTrue("base64 was ${parts[1]}", parts[1].startsWith("AAAAC3NzaC1lZDI1NTE5AAAAI"))
    }

    @Test
    fun `blob round-trips to the type string and the 32-byte public point`() {
        val id = Ed25519Identity.fromSeed(seed)
        val blob = OpenSsh.ed25519Blob(id.rawPublic)
        val din = DataInputStream(ByteArrayInputStream(blob))

        val typeLen = din.readInt()
        val type = ByteArray(typeLen).also { din.readFully(it) }
        assertEquals("ssh-ed25519", String(type, Charsets.US_ASCII))

        val keyLen = din.readInt()
        assertEquals(32, keyLen)
        val key = ByteArray(keyLen).also { din.readFully(it) }
        assertEquals(Base64.getEncoder().encodeToString(id.rawPublic),
                     Base64.getEncoder().encodeToString(key))
        assertEquals(0, din.available())
    }

    @Test
    fun `fingerprint is SHA256 of the blob, 43 unpadded base64 chars`() {
        val id = Ed25519Identity.fromSeed(seed)
        val fp = id.fingerprint()
        assertTrue(fp.startsWith("SHA256:"))
        assertEquals(43, fp.removePrefix("SHA256:").length) // 32 bytes -> 43 chars, no '='
    }

    @Test
    fun `derivation from a seed is deterministic`() {
        val a = Ed25519Identity.fromSeed(seed)
        val b = Ed25519Identity.fromSeedB64(a.seedBase64())
        assertEquals(a.openSshPublicKey(), b.openSshPublicKey())
        assertEquals(a.fingerprint(), b.fingerprint())
    }

    @Test
    fun `distinct seeds yield distinct keys`() {
        val a = Ed25519Identity.fromSeed(ByteArray(32) { 1 })
        val b = Ed25519Identity.fromSeed(ByteArray(32) { 2 })
        assertNotEquals(a.fingerprint(), b.fingerprint())
    }
}
