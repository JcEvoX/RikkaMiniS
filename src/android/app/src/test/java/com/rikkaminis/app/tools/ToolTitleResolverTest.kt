package com.rikkaminis.app.tools

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [fix/da1022-G] The tool_title resolution moved BEFORE the try block
 * (FileWriteTool pattern) so the catch arms have a title to report. The
 * resolution itself is pure — pinned here on the JVM with a real org.json
 * (Maven jar, not the sandbox stub).
 *
 * Expectations are literals, nothing derived from production constants.
 */
class ToolTitleResolverTest {

    @Test
    fun `valid title passes through`() {
        assertEquals(
            "读取配置文件 config.json",
            resolveToolTitle("""{"path":"/a.md","tool_title":"读取配置文件 config.json"}""", "file_read"),
        )
    }

    @Test
    fun `missing title falls back to the tool name literal`() {
        assertEquals("file_read", resolveToolTitle("""{"path":"/a.md"}""", "file_read"))
    }

    @Test
    fun `malformed json falls back to the literal`() {
        // Unterminated string — JSONObject construction throws, runCatching falls back.
        assertEquals("file_edit", resolveToolTitle("""{"tool_title": "broken""", "file_edit"))
    }

    @Test
    fun `non-json input falls back to the literal`() {
        assertEquals("memory_write", resolveToolTitle("not json at all", "memory_write"))
        assertEquals("memory_get", resolveToolTitle("[]", "memory_get"))
    }

    @Test
    fun `memory literals are the fallbacks for the memory tools`() {
        assertEquals(
            "remembering earlier turns",
            resolveToolTitle("""{"keywords":"cookie","tool_title":"remembering earlier turns"}""", "memory_get"),
        )
        assertEquals("memory_write", resolveToolTitle("""{"content":"x"}""", "memory_write"))
    }
}
