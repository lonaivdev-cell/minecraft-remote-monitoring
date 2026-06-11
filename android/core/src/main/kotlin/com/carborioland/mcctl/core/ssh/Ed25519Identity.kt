package com.carborioland.mcctl.core.ssh

import net.i2p.crypto.eddsa.EdDSAPrivateKey
import net.i2p.crypto.eddsa.EdDSAPublicKey
import net.i2p.crypto.eddsa.spec.EdDSANamedCurveTable
import net.i2p.crypto.eddsa.spec.EdDSAPrivateKeySpec
import net.i2p.crypto.eddsa.spec.EdDSAPublicKeySpec
import net.schmizz.sshj.common.KeyType
import net.schmizz.sshj.userauth.keyprovider.KeyProvider
import java.security.PrivateKey
import java.security.PublicKey
import java.security.SecureRandom
import java.util.Base64

/**
 * The per-device SSH identity: an Ed25519 keypair generated on the phone, authorized on
 * the server like any other client key (and revocable there). Only the 32-byte seed is
 * persisted — the public key is derived deterministically — so the encrypted blob the
 * app stores is tiny.
 *
 * Uses the same Ed25519 implementation sshj itself uses (net.i2p.crypto.eddsa), so the
 * [keyProvider] keys are exactly the type sshj's public-key auth expects.
 */
class Ed25519Identity private constructor(
    val seed: ByteArray,
    val rawPublic: ByteArray,
) {
    /** `ssh-ed25519 AAAA… <comment>` — paste into the server's authorized_keys. */
    fun openSshPublicKey(comment: String = "mcctl-android"): String =
        OpenSsh.publicKeyLine(rawPublic, comment)

    /** `SHA256:…` fingerprint for the user to verify what they authorized. */
    fun fingerprint(): String = OpenSsh.sha256Fingerprint(rawPublic)

    /** Base64 of the seed — what the app stores (encrypted) and reloads with [fromSeedB64]. */
    fun seedBase64(): String = Base64.getEncoder().encodeToString(seed)

    /** An sshj [KeyProvider] for `client.authPublickey(user, provider)`. */
    fun keyProvider(): KeyProvider {
        val curve = EdDSANamedCurveTable.getByName(EdDSANamedCurveTable.ED_25519)
        val privSpec = EdDSAPrivateKeySpec(seed, curve)
        val priv: PrivateKey = EdDSAPrivateKey(privSpec)
        val pub: PublicKey = EdDSAPublicKey(EdDSAPublicKeySpec(privSpec.a, curve))
        return object : KeyProvider {
            override fun getPrivate(): PrivateKey = priv
            override fun getPublic(): PublicKey = pub
            override fun getType(): KeyType = KeyType.ED25519
        }
    }

    companion object {
        fun generate(random: SecureRandom = SecureRandom()): Ed25519Identity {
            val seed = ByteArray(32).also { random.nextBytes(it) }
            return fromSeed(seed)
        }

        fun fromSeed(seed: ByteArray): Ed25519Identity {
            require(seed.size == 32) { "Ed25519 seed must be 32 bytes" }
            val curve = EdDSANamedCurveTable.getByName(EdDSANamedCurveTable.ED_25519)
            val privSpec = EdDSAPrivateKeySpec(seed, curve)
            val pub = EdDSAPublicKey(EdDSAPublicKeySpec(privSpec.a, curve))
            return Ed25519Identity(seed.copyOf(), pub.abyte)
        }

        fun fromSeedB64(b64: String): Ed25519Identity = fromSeed(Base64.getDecoder().decode(b64))
    }
}
