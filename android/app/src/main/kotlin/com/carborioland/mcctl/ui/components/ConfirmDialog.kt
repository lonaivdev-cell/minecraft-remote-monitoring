package com.carborioland.mcctl.ui.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.carborioland.mcctl.ui.theme.mc

/**
 * A themed confirm dialog. For a [typedConfirm] action (e.g. restore) the user must type
 * the exact token before the confirm button enables — the same friction the CLI's typed
 * confirm gives a world-replacing operation.
 */
@Composable
fun ConfirmDialog(
    title: String,
    body: String,
    confirmText: String,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
    destructive: Boolean = false,
    typedConfirm: String? = null,
) {
    var typed by remember { mutableStateOf("") }
    val canConfirm = typedConfirm == null || typed.trim() == typedConfirm

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            McButton(
                text = confirmText,
                onClick = onConfirm,
                kind = if (destructive) BtnKind.Danger else BtnKind.Primary,
                enabled = canConfirm,
            )
        },
        dismissButton = { McButton("Cancel", onDismiss, kind = BtnKind.Neutral) },
        title = { Text(title, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface) },
        text = {
            Column {
                Text(body, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.mc.dim)
                if (typedConfirm != null) {
                    McTextField(
                        label = "Type \"$typedConfirm\" to confirm",
                        value = typed,
                        onValueChange = { typed = it },
                        modifier = Modifier.padding(top = 12.dp),
                    )
                }
            }
        },
        containerColor = MaterialTheme.colorScheme.surface,
    )
}
