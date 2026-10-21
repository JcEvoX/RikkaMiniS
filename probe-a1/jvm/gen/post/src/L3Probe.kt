package probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * [dv20-a1 A-01/A-02] JVM probe: the two decision points of this batch, run
 * against REAL extracted production code (see make_harness.py) — not a
 * re-implementation.
 *
 *  - A-02: the single membership predicate for group member ids;
 *  - A-01: the artifact face of a credentials-free backup (redaction, the
 *    fail-closed drop, the verbatim pass-through and the null-transform
 *    opt-out).
 *
 * Every expectation is a hand-written literal; nothing is derived from the
 * code under test, so a weakened predicate cannot move an assertion.
 */
class L3Probe {

    private val serversJson = """
        {"mcpServers":{
          "http-one":{"url":"https://mcp.example.com/sse?key=SUPERSECRET&mode=fast",
                      "headers":{"Authorization":"Bearer SUPERSECRET","X-Trace":"keep-me"},
                      "enabled":true},
          "stdio-one":{"command":"npx","args":["-y","pkg","--token=SUPERSECRET"],
                       "env":{"API_KEY":"SUPERSECRET"},"enabled":true}}}
    """.trimIndent()

    private fun tmpDir(): File {
        val d = File.createTempFile("l3probe", ".dir")
        d.delete()
        d.mkdirs()
        return d
    }

    private fun pack(
        root: File,
        files: List<ArtifactBackupScope.SelectedFile>,
        transform: ((String, ByteArray) -> ByteArray?)?,
        dropped: MutableList<String>?,
    ): Map<String, ByteArray> {
        val out = File.createTempFile("l3probe", ".zip")
        FileOutputStream(out).use { fos ->
            ZipOutputStream(fos).use { zos ->
                ArtifactArchiver.writeEntries(zos, root, files, "mcp/", transform, dropped)
            }
        }
        val zip = out.readBytes()
        out.delete()
        val entries = LinkedHashMap<String, ByteArray>()
        ZipInputStream(ByteArrayInputStream(zip)).use { zis ->
            var e: ZipEntry? = zis.nextEntry
            while (e != null) {
                val buf = ByteArrayOutputStream()
                val chunk = ByteArray(4096)
                while (true) {
                    val n = zis.read(chunk)
                    if (n <= 0) break
                    buf.write(chunk, 0, n)
                }
                entries[e.name] = buf.toByteArray()
                zis.closeEntry()
                e = zis.nextEntry
            }
        }
        return entries
    }

    private fun sel(vararg rel: String) =
        rel.map { ArtifactBackupScope.SelectedFile(it) }

    // ---- A-02 -----------------------------------------------------------

    @Test
    fun t1SentinelIdsAreKnownGroupMembers() {
        assertTrue(
            "the virtual System voice member must be known without being persisted",
            VoiceSlice.isKnownGroupMemberId("__builtin_system_speech__/system-tts", emptySet()),
        )
        assertTrue(VoiceSlice.isKnownGroupMemberId("__builtin_system_speech__", emptySet()))
        assertTrue(VoiceSlice.isKnownGroupMemberId("inst/model", setOf("inst/model")))
        assertFalse(
            "fail closed: an id that is neither persisted nor System stays unknown",
            VoiceSlice.isKnownGroupMemberId("inst/ghost", setOf("inst/model")),
        )
    }

    // ---- A-01: the archive face ----------------------------------------

    @Test
    fun t2CredentialsNeverReachTheArchive() {
        val dir = tmpDir()
        File(dir, "servers.json").writeText(serversJson)
        val dropped = ArrayList<String>()
        val got = pack(dir, sel("servers.json"), RedactorSlice::redactMcpArtifactEntry, dropped)

        assertEquals(setOf("mcp/servers.json"), got.keys)
        val text = String(got["mcp/servers.json"]!!, Charsets.UTF_8)
        assertFalse("no secret may survive in the archive", text.contains("SUPERSECRET"))
        assertTrue("blanking keeps the key names the user must refill",
            text.contains("\"X-Trace\""))
        assertTrue("benign fields survive", text.contains("\"enabled\""))
        assertTrue("a redacted entry is not a dropped entry", dropped.isEmpty())
    }

    @Test
    fun t3UnparseableServerConfigDropsTheEntry() {
        val dir = tmpDir()
        File(dir, "servers.json").writeText("{ not json at all")
        val dropped = ArrayList<String>()
        val got = pack(dir, sel("servers.json"), RedactorSlice::redactMcpArtifactEntry, dropped)

        assertEquals("bytes that cannot be parsed must never be archived verbatim", 0, got.size)
        assertEquals(listOf("servers.json"), dropped)
    }

    @Test
    fun t4NonServerArtifactsPassVerbatim() {
        val dir = tmpDir()
        val jsonl = "{\"level\":\"info\"}\n"
        val notes = "# notes\n"
        File(dir, "data.jsonl").writeText(jsonl)
        File(dir, "notes.md").writeText(notes)
        val dropped = ArrayList<String>()
        val got = pack(dir, sel("data.jsonl", "notes.md"),
            RedactorSlice::redactMcpArtifactEntry, dropped)

        assertEquals(setOf("mcp/data.jsonl", "mcp/notes.md"), got.keys)
        assertEquals(jsonl, String(got["mcp/data.jsonl"]!!, Charsets.UTF_8))
        assertEquals(notes, String(got["mcp/notes.md"]!!, Charsets.UTF_8))
        assertTrue("only the credential carrier is rewritten", dropped.isEmpty())
    }

    @Test
    fun t5NullTransformWritesEveryEntryVerbatim() {
        val dir = tmpDir()
        File(dir, "servers.json").writeText(serversJson)
        val got = pack(dir, sel("servers.json"), null, null)

        assertEquals(setOf("mcp/servers.json"), got.keys)
        assertTrue(
            "a caller that passes no hook keeps the pre-dv20-a1 behaviour",
            String(got["mcp/servers.json"]!!, Charsets.UTF_8).contains("SUPERSECRET"),
        )
    }
}
