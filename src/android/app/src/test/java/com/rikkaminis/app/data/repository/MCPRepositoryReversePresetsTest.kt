package com.rikkaminis.app.data.repository

import com.rikkaminis.app.data.repository.MCPRepository.Companion.DEFAULT_REVERSE_MCP_SERVERS
import com.rikkaminis.app.data.repository.MCPRepository.MCPServerConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [T-reverse-mcp-presets] Coverage for the add-if-missing seed of the
 * reverse-engineering MCP backends ported from the 玄星/XuanXing 二开.
 *
 * The contract that matters: presets are non-destructive — a user's own
 * server (or a user-edited preset) is never overwritten or removed, and the
 * presets themselves always ship DISABLED so a user without MT 管理器 /
 * 玄星逆核 / ProxyPin is never affected.
 */
class MCPRepositoryReversePresetsTest {

    @Test
    fun `empty list seeds every preset disabled`() {
        val seeded = MCPRepository.seedDefaultReverseServers(emptyList())
        assertEquals(DEFAULT_REVERSE_MCP_SERVERS.size, seeded.size)
        assertEquals(DEFAULT_REVERSE_MCP_SERVERS.map { it.id }, seeded.map { it.id })
        assertTrue(seeded.all { !it.enabled })
    }

    @Test
    fun `presets are local http endpoints`() {
        DEFAULT_REVERSE_MCP_SERVERS.forEach { preset ->
            assertTrue("${preset.id} must be a 127.0.0.1 URL", preset.url!!.startsWith("http://127.0.0.1:"))
            assertFalse(preset.isStdio)
        }
    }

    @Test
    fun `missing presets are appended without reordering the user's servers`() {
        val mine = MCPServerConfig(id = "MyServer", enabled = true, url = "http://127.0.0.1:1234/mcp")
        val seeded = MCPRepository.seedDefaultReverseServers(listOf(mine))
        assertEquals(listOf("MyServer") + DEFAULT_REVERSE_MCP_SERVERS.map { it.id }, seeded.map { it.id })
        assertEquals(mine, seeded.first())
    }

    @Test
    fun `user-edited preset survives untouched`() {
        val edited = MCPServerConfig(
            id = DEFAULT_REVERSE_MCP_SERVERS.first().id,
            note = "我的端口",
            enabled = true,
            url = "http://127.0.0.1:9999/mcp",
        )
        val seeded = MCPRepository.seedDefaultReverseServers(listOf(edited))
        assertEquals(edited, seeded.first())
        assertEquals(DEFAULT_REVERSE_MCP_SERVERS.size, seeded.size)
    }

    @Test
    fun `already complete list is returned as the same instance`() {
        val complete = DEFAULT_REVERSE_MCP_SERVERS.toMutableList()
        val seeded = MCPRepository.seedDefaultReverseServers(complete)
        assertSame(complete, seeded)
    }
}