package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.core.ssh.HostKeyStatus
import com.carborioland.mcctl.data.HostKeyPrompt
import com.carborioland.mcctl.ui.theme.TerminalTextStyle
import com.carborioland.mcctl.ui.theme.mc

/**
 * The trust-on-first-use dialog. A NEW key asks the user to verify the fingerprint
 * out-of-band; a CHANGED key warns loudly (possible man-in-the-middle, or a rebuilt box).
 */
@Composable
fun HostKeyDialog(prompt: HostKeyPrompt, onAccept: () -> Unit, onReject: () -> Unit) {
    val changed = prompt.status == HostKeyStatus.CHANGED
    AlertDialog(
        onDismissRequest = onReject,
        confirmButton = {
            McButton(
                text = if (changed) "Trust anyway" else "Trust & connect",
                onClick = onAccept,
                kind = if (changed) BtnKind.Danger else BtnKind.Primary,
            )
        },
        dismissButton = { McButton("Cancel", onReject, kind = BtnKind.Neutral) },
        icon = null,
        title = {
            Text(
                if (changed) "⚠ HOST KEY CHANGED" else "Verify host key",
                style = MaterialTheme.typography.titleMedium,
                color = if (changed) MaterialTheme.mc.danger else MaterialTheme.colorScheme.onSurface,
            )
        },
        text = {
            Column {
                Text(
                    if (changed) {
                        "The key for ${prompt.host}:${prompt.port} is different from the one you " +
                            "trusted before. This can mean the box was rebuilt — or that someone is " +
                            "intercepting the connection. Only continue if you know why it changed."
                    } else {
                        "First time connecting to ${prompt.host}:${prompt.port}. Confirm this " +
                            "fingerprint matches the server (e.g. run ssh-keygen -lf on the host)."
                    },
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.mc.dim,
                )
                Text(
                    "${prompt.keyType}\n${prompt.fingerprint}",
                    style = TerminalTextStyle,
                    color = MaterialTheme.colorScheme.onSurface,
                    overflow = TextOverflow.Visible,
                    modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                )
            }
        },
        containerColor = MaterialTheme.colorScheme.surface,
    )
}
