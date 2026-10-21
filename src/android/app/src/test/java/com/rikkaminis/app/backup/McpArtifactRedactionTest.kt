package com.rikkaminis.app.backup

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

/**
 * [dv20-a1 A-01] The artifact (output-asset) face of a credentials-free
 * backup must not carry the MCP server config in the clear.
 *
 * Before the fix the archive was assembled straight off disk: the artifact
 * loop ignored `includeSecrets` entirely, so `mcp-servers/servers.json` —
 * MCPRepository's on-disk config, `{"mcpServers":{"<id>":{…}}}` with live
 * `headers` / `env` / `--token=` args / URL query keys — rode the `"mcp/"`
 * prefix into the zip while Stage 5 was busy blanking those very fields out
 * of the `mcpServers` JSON section of the same file.
 *
 * Every expected value here is a hand-written literal: nothing is derived
 * from the code under test, so a mutated redactor cannot move an assertion
 * to keep itself green.
 */
class McpArtifactRedactionTest {

    /** A servers.json carrying every credential carrier the redactor knows:
     *  an HTTP server (header + URL query key) and a stdio server (env +
     *  `--token=` arg), plus benign fields that MUST survive. */
    private val serversJson = """
        {
          "mcpServers": {
            "http-one": {
              "url": "https://mcp.example.com/sse?key=SUPERSECRET&mode=fast",
              "headers": { "Authorization": "Bearer SUPERSECRET", "X-Trace": "SUPERSECRET" },
              "note": "keep me",
              "enabled": true
            },
            "stdio-one": {
              "command": "npx",
              "args": ["-y", "some-server", "--token=SUPERSECRET", "--port=8080"],
              "env": { "API_TOKEN": "SUPERSECRET", "LOG_LEVEL": "info" }
            }
          }
        }
    """.trimIndent()

    private fun redact(rel: String, text: String): JSONObject? {
        val out = ConfigBackup.redactMcpArtifactEntry(rel, text.toByteArray(Charsets.UTF_8))
            ?: return null
        return JSONObject(String(out, Charsets.UTF_8))
    }

    // ── The redactor ─────────────────────────────────────────────────────

    @Test
    fun serversJsonLosesEveryCredentialCarrier() {
        val obj = redact("servers.json", serversJson)
        assertNotNull(obj)
        assertFalse(obj.toString().contains("SUPERSECRET"))

        val http = obj!!.getJSONObject("mcpServers").getJSONObject("http-one")
        // Values blanked, KEY NAMES kept — the entry still imports and the
        // user can see exactly which credential to refill.
        assertEquals("", http.getJSONObject("headers").getString("Authorization"))
        assertEquals("", http.getJSONObject("headers").getString("X-Trace"))
        assertEquals(
            "[redacted:11]",
            http.getString("url").substringAfter("key=").substringBefore("&"),
        )
        assertTrue(http.getString("url").endsWith("mode=fast"))

        val stdio = obj.getJSONObject("mcpServers").getJSONObject("stdio-one")
        assertEquals("", stdio.getJSONObject("env").getString("API_TOKEN"))
        assertEquals("", stdio.getJSONObject("env").getString("LOG_LEVEL"))
        assertEquals("--token=", stdio.getJSONArray("args").getString(2))
    }

    @Test
    fun serversJsonKeepsEveryNonSecretField() {
        val obj = redact("servers.json", serversJson)!!
        val http = obj.getJSONObject("mcpServers").getJSONObject("http-one")
        assertEquals("keep me", http.getString("note"))
        assertTrue(http.getBoolean("enabled"))
        assertEquals("https://mcp.example.com/sse", http.getString("url").substringBefore("?"))

        val stdio = obj.getJSONObject("mcpServers").getJSONObject("stdio-one")
        assertEquals("npx", stdio.getString("command"))
        assertEquals("-y", stdio.getJSONArray("args").getString(0))
        assertEquals("some-server", stdio.getJSONArray("args").getString(1))
        assertEquals("--port=8080", stdio.getJSONArray("args").getString(3))
    }

    @Test
    fun nestedServersJsonIsAlsoRedacted() {
        val obj = redact("backup/servers.json", serversJson)
        assertNotNull(obj)
        assertFalse(obj.toString().contains("SUPERSECRET"))
    }

    @Test
    fun otherMcpArtifactsPassVerbatim() {
        val jsonl = """{"topic":"mcp-memory","value":"SUPERSECRET is a word here"}"""
        val out = ConfigBackup.redactMcpArtifactEntry("knowledge.jsonl", jsonl.toByteArray())
        assertEquals(jsonl, String(out!!, Charsets.UTF_8))
    }

    @Test
    fun unparseableServersJsonIsDroppedNotCopiedVerbatim() {
        // Fail closed: bytes we cannot redact are bytes we must not export.
        assertNull(ConfigBackup.redactMcpArtifactEntry("servers.json", "{ not json".toByteArray()))
    }

    // ── The archive face ─────────────────────────────────────────────────

    @Test
    fun writeEntriesDropsRejectedEntriesAndReportsThem() {
        val root = mcpRoot()
        try {
            File(root, "servers.json").writeText(serversJson)
            File(root, "knowledge.jsonl").writeText("""{"k":1}""" + "\n")
            val files = listOf(
                ArtifactBackupScope.SelectedFile("servers.json", 11L),
                ArtifactBackupScope.SelectedFile("knowledge.jsonl", 8L),
            )
            val out = File(root, "out.zip")
            val dropped = ArrayList<String>()

            val byteCount = withZip(out) { zos ->
                ArtifactArchiver.writeEntries(
                    zos, root, files, "mcp/",
                    // Reject ONE entry to pin the fail-closed semantics: a null
                    // result drops it, a non-null result is archived.
                    transform = { rel, bytes -> if (rel == "servers.json") null else bytes },
                    droppedSink = dropped,
                )
            }

            assertEquals(listOf("servers.json"), dropped)
            assertEquals(listOf("mcp/knowledge.jsonl"), zipEntryNames(out))
            assertEquals(8L, byteCount)
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun archivedServersJsonIsRedactedAndKeepsItsEntryName() {
        val root = mcpRoot()
        try {
            File(root, "servers.json").writeText(serversJson)
            File(root, "knowledge.jsonl").writeText("""{"topic":"keep"}""" + "\n")
            val files = listOf(
                ArtifactBackupScope.SelectedFile("servers.json", 11L),
                ArtifactBackupScope.SelectedFile("knowledge.jsonl", 18L),
            )
            val out = File(root, "out.zip")
            val dropped = ArrayList<String>()

            val byteCount = withZip(out) { zos ->
                ArtifactArchiver.writeEntries(
                    zos, root, files, "mcp/",
                    transform = { rel, bytes -> ConfigBackup.redactMcpArtifactEntry(rel, bytes) },
                    droppedSink = dropped,
                )
            }

            // Nothing dropped, both entries present under their original names.
            assertEquals(emptyList<String>(), dropped)
            assertEquals(
                listOf("mcp/knowledge.jsonl", "mcp/servers.json"),
                zipEntryNames(out).sorted(),
            )
            assertTrue(byteCount > 0)

            // The archived bytes are the REDACTED document, not the on-disk one.
            val entryText = zipEntryText(out, "mcp/servers.json")
            assertFalse(entryText.contains("SUPERSECRET"))
            assertTrue(entryText.contains("mcpServers"))
            assertEquals(
                "npx",
                JSONObject(entryText).getJSONObject("mcpServers")
                    .getJSONObject("stdio-one").getString("command"),
            )

            // The untouched artifact is archived verbatim.
            assertEquals(
                File(root, "knowledge.jsonl").readText(),
                zipEntryText(out, "mcp/knowledge.jsonl"),
            )

            // Restore routing is unchanged: entries still land in the mcp root.
            val dest = File.createTempFile("mcp-dest", "").apply { delete(); mkdirs() }
            try {
                val written = ArtifactArchiver.extractZipByPrefix(out, mapOf("mcp" to dest))
                assertEquals(2, written)
                assertFalse(File(dest, "servers.json").readText().contains("SUPERSECRET"))
            } finally {
                dest.deleteRecursively()
            }
        } finally {
            root.deleteRecursively()
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────

    private fun mcpRoot(): File = File.createTempFile("mcp-root", "").apply { delete(); mkdirs() }

    private fun withZip(out: File, block: (ZipOutputStream) -> Long): Long {
        out.parentFile?.mkdirs()
        var n = 0L
        java.io.FileOutputStream(out).use { fos ->
            ZipOutputStream(fos.buffered()).use { zos -> n = block(zos) }
        }
        return n
    }

    private fun zipEntryNames(zip: File): List<String> {
        val names = ArrayList<String>()
        ZipFile(zip).use { zf ->
            val e = zf.entries()
            while (e.hasMoreElements()) {
                val entry = e.nextElement()
                if (!entry.isDirectory) names.add(entry.name)
            }
        }
        return names
    }

    private fun zipEntryText(zip: File, name: String): String {
        ZipFile(zip).use { zf ->
            val entry: ZipEntry = zf.getEntry(name) ?: error("missing entry $name")
            return zf.getInputStream(entry).use { it.readBytes().toString(Charsets.UTF_8) }
        }
    }
}
