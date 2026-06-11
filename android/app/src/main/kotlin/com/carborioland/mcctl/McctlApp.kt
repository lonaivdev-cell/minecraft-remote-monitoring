package com.carborioland.mcctl

import android.app.Application
import com.carborioland.mcctl.di.AppContainer
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

/**
 * Application entry point. Swaps Android's stripped-down "BC" provider for the full
 * BouncyCastle build so sshj can negotiate modern ciphers, KEX and host-key algorithms
 * with a real OpenSSH server (the platform provider lacks several of them).
 */
class McctlApp : Application() {

    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        Security.removeProvider("BC")
        Security.insertProviderAt(BouncyCastleProvider(), 1)
        container = AppContainer(this)
    }
}
