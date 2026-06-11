package com.carborioland.mcctl.core.ssh

import net.schmizz.sshj.common.Buffer
import net.schmizz.sshj.common.KeyType
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import java.security.MessageDigest
import java.security.PublicKey
import java.util.Base64

/** Whether a presented host key is new, matches what we trusted, or changed under us. */
enum class HostKeyStatus { NEW, MATCH, CHANGED }

/** Persisted trust store for host fingerprints (trust-on-first-use). App-provided. */
interface KnownHostsStore {
    fun fingerprintFor(host: String, port: Int): String?
    fun remember(host: String, port: Int, fingerprint: String)
}

/**
 * The decision hook for a NEW or CHANGED host key. The app shows the fingerprint and
 * asks the user; returning true accepts (and the fingerprint is then remembered). A
 * MATCH never reaches the gate — it is accepted silently.
 */
fun interface HostKeyGate {
    fun accept(host: String, port: Int, keyType: String, fingerprintSha256: String, status: HostKeyStatus): Boolean
}

/**
 * A [HostKeyVerifier] implementing SSH's standard TOFU policy on top of an app store and
 * a user gate. The same SHA256 fingerprint format `ssh-keygen -l` prints is surfaced, so
 * a careful user can compare it against the server out-of-band.
 */
class GatedHostKeyVerifier(
    private val store: KnownHostsStore,
    private val gate: HostKeyGate,
) : HostKeyVerifier {

    override fun verify(hostname: String, port: Int, key: PublicKey): Boolean {
        val fp = sha256Fingerprint(key)
        val stored = store.fingerprintFor(hostname, port)
        val status = when (stored) {
            null -> HostKeyStatus.NEW
            fp -> HostKeyStatus.MATCH
            else -> HostKeyStatus.CHANGED
        }
        if (status == HostKeyStatus.MATCH) return true
        val accepted = gate.accept(hostname, port, KeyType.fromKey(key).toString(), fp, status)
        if (accepted) store.remember(hostname, port, fp)
        return accepted
    }

    override fun findExistingAlgorithms(hostname: String, port: Int): List<String> = emptyList()

    companion object {
        /** `SHA256:…` of the SSH wire encoding of any host key type (RSA/ECDSA/Ed25519). */
        fun sha256Fingerprint(key: PublicKey): String {
            val blob = Buffer.PlainBuffer().putPublicKey(key).compactData
            val digest = MessageDigest.getInstance("SHA-256").digest(blob)
            return "SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(digest)
        }
    }
}
