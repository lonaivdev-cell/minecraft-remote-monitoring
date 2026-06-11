package com.carborioland.mcctl.ui

import androidx.compose.runtime.Composable
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel

/**
 * Builds a [ViewModel] from a lambda, so screens can construct VMs with manual-DI
 * dependencies (no Hilt). Keyed by the VM class, so each screen gets its own instance
 * that survives recomposition and configuration changes.
 */
@Composable
inline fun <reified VM : ViewModel> rememberVm(crossinline create: () -> VM): VM =
    viewModel(
        factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T = create() as T
        },
    )
