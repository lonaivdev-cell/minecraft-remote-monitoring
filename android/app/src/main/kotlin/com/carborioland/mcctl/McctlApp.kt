package com.carborioland.mcctl

import android.app.Application
import com.carborioland.mcctl.core.ssh.SecurityProvider
import com.carborioland.mcctl.di.AppContainer

/**
 * Application entry point. Installs the full BouncyCastle security provider (so sshj can
 * negotiate modern ciphers/KEX/host-key algorithms) and builds the DI container.
 */
class McctlApp : Application() {

    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        SecurityProvider.install()
        container = AppContainer(this)
    }
}
