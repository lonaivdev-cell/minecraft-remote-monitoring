package com.carborioland.mcctl.core.ssh

import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.Base64

/**
 * OpenSSH wire-format encoding for Ed25519 public keys — the exact bytes that go into
 * `~/.ssh/authorized_keys` on the server. Pure and unit-tested: the runtime crypto lives
 * in [Ed25519Identity], but the byte layout a server must accept is verified here.
 *
 * Layout (RFC 8709 / PROTOCOL.key): an SSH "string" is a 4-byte big-endian length
 * followed by that many bytes. A blob is `string "ssh-ed25519"` then `string <32-byte
 * point>`, base64-encoded, prefixed with the key type and an optional comment.
 */
object OpenSsh {

    const val ED25519_TYPE = "ssh-ed25519"

    /** The raw blob (pre-base64) for an Ed25519 public point. */
    fun ed25519Blob(rawPublic: ByteArray): ByteArray {
        require(rawPublic.size == 32) { "Ed25519 public key must be 32 bytes, got ${rawPublic.size}" }
        val out = ByteArrayOutputStream()
        writeString(out, ED25519_TYPE.toByteArray(Charsets.US_ASCII))
        writeString(out, rawPublic)
        return out.toByteArray()
    }

    /** A full `authorized_keys` line: `ssh-ed25519 <base64> <comment>`. */
    fun publicKeyLine(rawPublic: ByteArray, comment: String = ""): String {
        val b64 = Base64.getEncoder().encodeToString(ed25519Blob(rawPublic))
        return if (comment.isBlank()) "$ED25519_TYPE $b64" else "$ED25519_TYPE $b64 $comment"
    }

    /**
     * The OpenSSH SHA-256 fingerprint of the key, e.g. `SHA256:abc…` (no `=` padding),
     * matching `ssh-keygen -lf`. Shown to the user so they can eyeball what they authorized.
     */
    fun sha256Fingerprint(rawPublic: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(ed25519Blob(rawPublic))
        val b64 = Base64.getEncoder().withoutPadding().encodeToString(digest)
        return "SHA256:$b64"
    }

    private fun writeString(out: ByteArrayOutputStream, data: ByteArray) {
        val n = data.size
        out.write((n ushr 24) and 0xff)
        out.write((n ushr 16) and 0xff)
        out.write((n ushr 8) and 0xff)
        out.write(n and 0xff)
        out.write(data)
    }
}
