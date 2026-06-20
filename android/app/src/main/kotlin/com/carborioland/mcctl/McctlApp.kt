package com.carborioland.mcctl

import android.app.Application
import com.carborioland.mcctl.core.ssh.SecurityProvider
import com.carborioland.mcctl.di.AppContainer
import com.carborioland.mcctl.push.McctlNotifications
import com.carborioland.mcctl.push.PushScheduler
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

/**
 * Application entry point. Installs the full BouncyCastle security provider (so sshj can
 * negotiate modern ciphers/KEX/host-key algorithms), builds the DI container, and reconciles
 * the background ntfy push schedule with the saved profile.
 */
class McctlApp : Application() {

    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        SecurityProvider.install()
        container = AppContainer(this)
        McctlNotifications.ensureChannels(this)
        container.appScope.launch {
            PushScheduler.apply(this@McctlApp, container.profileStore.profile.first())
        }
    }
}
