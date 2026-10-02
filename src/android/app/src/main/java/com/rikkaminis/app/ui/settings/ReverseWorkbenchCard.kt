package com.rikkaminis.app.ui.settings

import android.widget.Toast
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.rikkaminis.app.R
import com.rikkaminis.app.data.repository.MCPRepository
import com.rikkaminis.app.data.repository.MCPRepository.Companion.REVERSE_MT_APK_SERVER_ID
import com.rikkaminis.app.data.repository.MCPRepository.Companion.REVERSE_PROXYPIN_SERVER_ID
import com.rikkaminis.app.data.repository.MCPRepository.Companion.REVERSE_SOMCP_SERVER_ID
import com.rikkaminis.app.data.repository.MCPRepository.MCPServerConfig
import com.rikkaminis.app.util.isPackageInstalled
import com.rikkaminis.app.util.launchOrOpenMarket
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

// Related app package names, for one-tap launch and install detection.
private const val PKG_MT = "bin.mt.plus"
private const val PKG_MT_CANARY = "bin.mt.plus.canary"
private const val PKG_SOMCP = "com.soreverse.mcp"
private const val PKG_PROXYPIN = "com.network.proxy"

/**
 * [T-reverse-workbench] Reverse-engineering workbench card, ported from the
 * 玄星/XuanXing 二开 `ReverseWorkbenchCard`.
 *
 * Sits at the top of the MCP settings page and does three things:
 *  1. One-tap launch of MT 管理器 / SOMCP / ProxyPin (store listing if missing).
 *  2. Per-backend toggle + editable port, written straight through to the
 *     seeded [MCPRepository.DEFAULT_REVERSE_MCP_SERVERS] entries.
 *  3. "Auto-detect & connect": probes the well-known local ports of each
 *     backend and, on a hit, rewrites the URL + enables the server.
 *
 * It never probes on its own — probing only happens when the user taps the
 * button, so a user without any of these apps sees zero traffic.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ReverseWorkbenchCard(
    mcpRepository: MCPRepository,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val servers by mcpRepository.servers.collectAsState()
    val scope = rememberCoroutineScope()
    var probing by remember { mutableStateOf(false) }

    fun serverOf(id: String): MCPServerConfig? = servers.firstOrNull { it.id == id }

    fun setEnabled(id: String, enabled: Boolean) = mcpRepository.setEnabled(id, enabled)

    // A user may change the listening port inside the backend app; let them edit
    // it here and write the new port back into the server's URL.
    fun updatePort(id: String, raw: String) {
        val server = serverOf(id) ?: return
        val url = server.url ?: return
        val port = raw.filter { it.isDigit() }.take(5)
        if (port.isBlank()) return
        mcpRepository.update(server.copy(url = replacePort(url, port)))
    }

    fun autoProbeAndConnect() {
        if (probing) return
        probing = true
        scope.launch {
            val candidates = listOf(
                REVERSE_MT_APK_SERVER_ID to listOf(8787, 8788, 8080, 9999),
                REVERSE_SOMCP_SERVER_ID to listOf(8000, 8001, 8080, 9000),
                REVERSE_PROXYPIN_SERVER_ID to listOf(9010, 9011, 9020),
            )
            val hits = withContext(Dispatchers.IO) {
                val found = LinkedHashMap<String, Int>()
                for ((id, ports) in candidates) {
                    for (p in ports) {
                        if (isPortOpen("127.0.0.1", p)) {
                            found[id] = p
                            break
                        }
                    }
                }
                found
            }
            if (hits.isEmpty()) {
                Toast.makeText(
                    context,
                    context.getString(R.string.reverse_workbench_probe_none),
                    Toast.LENGTH_SHORT,
                ).show()
            } else {
                for ((id, port) in hits) {
                    val server = serverOf(id) ?: continue
                    val url = server.url ?: continue
                    mcpRepository.update(
                        server.copy(url = replacePort(url, port.toString()), enabled = true)
                    )
                }
                val names = hits.keys.joinToString(", ") { backendName(it) }
                Toast.makeText(
                    context,
                    context.getString(R.string.reverse_workbench_probe_ok, names),
                    Toast.LENGTH_SHORT,
                ).show()
            }
            probing = false
        }
    }

    SettingsSection(
        header = stringResource(R.string.reverse_workbench_title),
        footer = stringResource(R.string.reverse_workbench_hint),
        modifier = modifier,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                text = stringResource(R.string.reverse_workbench_desc),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            FilledTonalButton(
                onClick = { autoProbeAndConnect() },
                enabled = !probing,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = if (probing) {
                        stringResource(R.string.reverse_workbench_probing)
                    } else {
                        stringResource(R.string.reverse_workbench_probe)
                    }
                )
            }
        }

        BackendRow(
            title = stringResource(R.string.reverse_workbench_mt_title),
            desc = stringResource(R.string.reverse_workbench_mt_desc),
            enabled = serverOf(REVERSE_MT_APK_SERVER_ID)?.enabled == true,
            port = portOf(serverOf(REVERSE_MT_APK_SERVER_ID)?.url, "8787"),
            onToggle = { setEnabled(REVERSE_MT_APK_SERVER_ID, it) },
            onPortChange = { updatePort(REVERSE_MT_APK_SERVER_ID, it) },
        )
        BackendRow(
            title = stringResource(R.string.reverse_workbench_somcp_title),
            desc = stringResource(R.string.reverse_workbench_somcp_desc),
            enabled = serverOf(REVERSE_SOMCP_SERVER_ID)?.enabled == true,
            port = portOf(serverOf(REVERSE_SOMCP_SERVER_ID)?.url, "8000"),
            onToggle = { setEnabled(REVERSE_SOMCP_SERVER_ID, it) },
            onPortChange = { updatePort(REVERSE_SOMCP_SERVER_ID, it) },
        )
        BackendRow(
            title = stringResource(R.string.reverse_workbench_proxypin_title),
            desc = stringResource(R.string.reverse_workbench_proxypin_desc),
            enabled = serverOf(REVERSE_PROXYPIN_SERVER_ID)?.enabled == true,
            port = portOf(serverOf(REVERSE_PROXYPIN_SERVER_ID)?.url, "9010"),
            onToggle = { setEnabled(REVERSE_PROXYPIN_SERVER_ID, it) },
            onPortChange = { updatePort(REVERSE_PROXYPIN_SERVER_ID, it) },
        )

        // One-tap launch of the matching app (or its store listing).
        FlowRow(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            OutlinedButton(onClick = {
                val pkg = if (context.isPackageInstalled(PKG_MT)) PKG_MT else PKG_MT_CANARY
                context.launchOrOpenMarket(pkg)
            }) {
                Text(
                    if (isMtInstalled(context)) {
                        stringResource(R.string.reverse_workbench_open, "MT")
                    } else {
                        stringResource(R.string.reverse_workbench_install, "MT")
                    }
                )
            }
            OutlinedButton(onClick = { context.launchOrOpenMarket(PKG_SOMCP) }) {
                Text(
                    if (context.isPackageInstalled(PKG_SOMCP)) {
                        stringResource(R.string.reverse_workbench_open, "SOMCP")
                    } else {
                        stringResource(R.string.reverse_workbench_install, "SOMCP")
                    }
                )
            }
            OutlinedButton(onClick = { context.launchOrOpenMarket(PKG_PROXYPIN) }) {
                Text(
                    if (context.isPackageInstalled(PKG_PROXYPIN)) {
                        stringResource(R.string.reverse_workbench_open, "ProxyPin")
                    } else {
                        stringResource(R.string.reverse_workbench_install, "ProxyPin")
                    }
                )
            }
        }
    }
}

@Composable
private fun BackendRow(
    title: String,
    desc: String,
    enabled: Boolean,
    port: String,
    onToggle: (Boolean) -> Unit,
    onPortChange: (String) -> Unit,
) {
    val status = stringResource(
        if (enabled) R.string.reverse_workbench_status_on else R.string.reverse_workbench_status_off
    )
    SettingsRow(
        title = title,
        subtitle = "$desc · $status",
        trailing = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = port,
                    onValueChange = onPortChange,
                    label = { Text(stringResource(R.string.reverse_workbench_port)) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.width(96.dp),
                )
                Spacer(Modifier.width(8.dp))
                Switch(checked = enabled, onCheckedChange = onToggle)
            }
        },
    )
}

private fun backendName(id: String): String = when (id) {
    REVERSE_MT_APK_SERVER_ID -> "MT"
    REVERSE_SOMCP_SERVER_ID -> "SOMCP"
    REVERSE_PROXYPIN_SERVER_ID -> "ProxyPin"
    else -> id
}

private fun isMtInstalled(context: android.content.Context): Boolean =
    context.isPackageInstalled(PKG_MT) || context.isPackageInstalled(PKG_MT_CANARY)

/** Probe whether a local TCP port is listening (300ms timeout — fast). */
private fun isPortOpen(host: String, port: Int): Boolean = runCatching {
    Socket().use { socket ->
        socket.connect(InetSocketAddress(host, port), 300)
        true
    }
}.getOrDefault(false)

/** Port from a url like `http://127.0.0.1:PORT/mcp`, or [fallback]. */
private fun portOf(url: String?, fallback: String): String {
    if (url.isNullOrBlank()) return fallback
    val m = Regex(":(\\d+)").find(url) ?: return fallback
    return m.groupValues[1].ifBlank { fallback }
}

/** Replace the port in [url]; append one after the host when absent. */
private fun replacePort(url: String, port: String): String {
    if (url.isBlank()) return "http://127.0.0.1:$port/mcp"
    val withPort = Regex("(://[^/:]+):\\d+")
    if (withPort.containsMatchIn(url)) {
        return withPort.replace(url) { m -> "${m.groupValues[1]}:$port" }
    }
    val hostOnly = Regex("(://[^/]+)")
    return hostOnly.replace(url) { m -> "${m.groupValues[1]}:$port" }
}