package com.carborioland.mcctl.core.ssh

import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

/**
 * Swaps Android's stripped-down "BC" provider for the full BouncyCastle build so sshj can
 * negotiate modern ciphers, KEX and host-key algorithms with a real OpenSSH server (the
 * platform provider lacks several of them). Call once at process start.
 *
 * Lives in :core so the app never has to depend on BouncyCastle directly — the classes are
 * still packaged via :core's runtime dependency.
 */
object SecurityProvider {
    fun install() {
        Security.removeProvider("BC")
        Security.insertProviderAt(BouncyCastleProvider(), 1)
    }
}
