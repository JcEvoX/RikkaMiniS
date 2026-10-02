package com.rikkaminis.app.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.outlined.Bolt
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.rikkaminis.app.R
import com.rikkaminis.app.data.QuickMessage
import com.rikkaminis.app.data.QuickMessagesStore
import kotlinx.coroutines.launch

/**
 * [feat/quick-messages] 玄星二开「快捷消息」管理页（MiniS 适配版）。
 *
 * 全局提示词模板库：一条 = 标题 + 正文。与技能 / MCP 并列，从设置 → Agent Runtime 进入。
 * 原版按助手订阅（quickMessageIds），MiniS 是单人格架构，因此这里是全局列表，
 * 会话输入框可直接取用（见 QuickMessagesSheet）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun QuickMessagesScreen(
    store: QuickMessagesStore,
    onBack: () -> Unit,
) {
    val messages by store.messages.collectAsState()
    val scope = rememberCoroutineScope()

    var showAdd by rememberSaveable { mutableStateOf(false) }
    var editTarget by remember { mutableStateOf<QuickMessage?>(null) }
    var deleteTarget by remember { mutableStateOf<QuickMessage?>(null) }

    SettingsScaffold(
        title = stringResource(R.string.quick_messages_title),
        onBack = onBack,
        floatingActionButton = {
            FloatingActionButton(onClick = { showAdd = true }) {
                Icon(
                    imageVector = Icons.Default.Add,
                    contentDescription = stringResource(R.string.quick_messages_new),
                )
            }
        },
    ) {
        if (messages.isEmpty()) {
            EmptyState()
        } else {
            SettingsSection {
                messages.forEachIndexed { index, message ->
                    QuickMessageSettingsRow(
                        message = message,
                        showDivider = index != messages.lastIndex,
                        onEdit = { editTarget = message },
                        onDelete = { deleteTarget = message },
                    )
                }
            }
        }
        Spacer(Modifier.height(32.dp))
    }

    if (showAdd) {
        EditQuickMessageDialog(
            title = stringResource(R.string.quick_messages_new),
            initial = null,
            onDismiss = { showAdd = false },
            onConfirm = { t, c ->
                scope.launch { store.add(t, c) }
                showAdd = false
            },
        )
    }

    editTarget?.let { target ->
        EditQuickMessageDialog(
            title = stringResource(R.string.quick_messages_edit),
            initial = target,
            onDismiss = { editTarget = null },
            onConfirm = { t, c ->
                scope.launch { store.update(target.id, t, c) }
                editTarget = null
            },
        )
    }

    deleteTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            title = { Text(stringResource(R.string.quick_messages_delete_title)) },
            text = {
                Text(
                    stringResource(
                        R.string.quick_messages_delete_message,
                        target.title.ifBlank { stringResource(R.string.quick_messages_untitled) },
                    )
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    scope.launch { store.remove(target.id) }
                    deleteTarget = null
                }) {
                    Text(
                        text = stringResource(R.string.common_delete),
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            },
            dismissButton = {
                TextButton(onClick = { deleteTarget = null }) {
                    Text(stringResource(R.string.common_cancel))
                }
            },
        )
    }
}

@Composable
private fun EmptyState() {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 96.dp, start = 32.dp, end = 32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Icon(
            imageVector = Icons.Outlined.Bolt,
            contentDescription = null,
            modifier = Modifier.size(44.dp),
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = stringResource(R.string.quick_messages_empty_title),
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = stringResource(R.string.quick_messages_empty_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun QuickMessageSettingsRow(
    message: QuickMessage,
    showDivider: Boolean,
    onEdit: () -> Unit,
    onDelete: () -> Unit,
) {
    var menuExpanded by remember { mutableStateOf(false) }

    SettingsRow(
        icon = Icons.Outlined.Bolt,
        iconColor = MaterialTheme.colorScheme.primary,
        title = message.title.ifBlank { stringResource(R.string.quick_messages_untitled) },
        subtitle = message.content.ifBlank { stringResource(R.string.quick_messages_empty_content) },
        showDivider = showDivider,
        trailing = {
            Box {
                IconButton(onClick = { menuExpanded = true }) {
                    Icon(
                        imageVector = Icons.Default.MoreVert,
                        contentDescription = stringResource(R.string.quick_messages_more_actions),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                DropdownMenu(
                    expanded = menuExpanded,
                    onDismissRequest = { menuExpanded = false },
                ) {
                    DropdownMenuItem(
                        text = { Text(stringResource(R.string.common_edit)) },
                        leadingIcon = { Icon(Icons.Default.Edit, contentDescription = null) },
                        onClick = {
                            menuExpanded = false
                            onEdit()
                        },
                    )
                    DropdownMenuItem(
                        text = {
                            Text(
                                text = stringResource(R.string.common_delete),
                                color = MaterialTheme.colorScheme.error,
                            )
                        },
                        leadingIcon = {
                            Icon(
                                imageVector = Icons.Default.Delete,
                                contentDescription = null,
                                tint = MaterialTheme.colorScheme.error,
                            )
                        },
                        onClick = {
                            menuExpanded = false
                            onDelete()
                        },
                    )
                }
            }
        },
    )
}

@Composable
internal fun EditQuickMessageDialog(
    title: String,
    initial: QuickMessage?,
    onDismiss: () -> Unit,
    onConfirm: (title: String, content: String) -> Unit,
) {
    var fieldTitle by rememberSaveable(initial?.id) { mutableStateOf(initial?.title ?: "") }
    var fieldContent by rememberSaveable(initial?.id) { mutableStateOf(initial?.content ?: "") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = fieldTitle,
                    onValueChange = { fieldTitle = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text(stringResource(R.string.quick_messages_field_title)) },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = fieldContent,
                    onValueChange = { fieldContent = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text(stringResource(R.string.quick_messages_field_content)) },
                    minLines = 4,
                    maxLines = 8,
                    supportingText = {
                        Text(
                            text = stringResource(R.string.quick_messages_field_content_hint),
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis,
                        )
                    },
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(fieldTitle.trim(), fieldContent.trim()) },
                enabled = fieldTitle.isNotBlank() && fieldContent.isNotBlank(),
            ) {
                Text(stringResource(R.string.common_save))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.common_cancel))
            }
        },
    )
}