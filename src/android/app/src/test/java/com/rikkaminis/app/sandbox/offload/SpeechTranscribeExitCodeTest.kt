package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileNotFoundException

/**
 * [dv18-A06] A transcribe payload is the exit code's only signal (ISSUE-A-06).
 *
 * `SpeechOffloadHandler.recognize` expresses every failure INSIDE the JSON it
 * returns — `"no result"`, `recognizer_creation_failed`, the `onError` message
 * mapping, or the latch-timeout sentence — and `cmdTranscribe` returned that
 * payload with exit 0 unconditionally: a caller checking `$?` could not tell a
 * failed transcription from a successful one.
 *
 * The payload list below is the recognizer's real vocabulary (drawn from
 * `recognize`'s own branches, including the `toString(2)` indented shape it
 * produces); [transcribeExitCode] is the real implementation, so the mapping —
 * not a copy of it — is what is under test. The source-shape arm then pins the
 * other half of the contract: the payload still reaches the caller
 * byte-for-byte, and the exit code is always derived, never a literal.
 */
class SpeechTranscribeExitCodeTest {

    private companion object {
        private const val SPEECH =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/SpeechOffloadHandler.kt"
    }

    private val errorPayloads = listOf(
        // The default when neither callback set a result.
        """{"error": "no result"}""",
        // Recognizer creation threw on the main-thread post.
        """{"error": "recognizer_creation_failed", "message": "Could not create SpeechRecognizer: null"}""",
        // The onError mapping (one entry per branch of the `when`).
        """{"error": "Audio recording error"}""",
        """{"error": "Client error"}""",
        """{"error": "Insufficient permissions (RECORD_AUDIO required)"}""",
        """{"error": "Network error"}""",
        """{"error": "Network timeout"}""",
        """{"error": "No speech recognized"}""",
        """{"error": "Recognizer busy"}""",
        """{"error": "Server error"}""",
        """{"error": "No speech input detected"}""",
        """{"error": "Too many recognition requests — the system recognizer service is saturated (orphaned sessions?)"}""",
        """{"error": "Recognition service disconnected"}""",
        """{"error": "Language not supported by the recognizer (requested: fr-FR)"}""",
        """{"error": "Unknown error (99)"}""",
        // The latch-timeout path.
        """{"error": "speech recognition timed out after 30s"}""",
        // `recognize` serialises with toString(2) — indented JSON must map alike.
        "{\n  \"error\": \"No speech recognized\"\n}",
    )

    private val textPayloads = listOf(
        """{"text": "hello world"}""",
        """{"text": "hello world", "alternatives": [{"text": "hello world", "confidence": "0.92"}, {"text": "hello", "confidence": "0.51"}]}""",
        """{"text": ""}""",
        // Indented, empty result with an explicit (empty) alternative list.
        "{\n  \"text\": \"\",\n  \"alternatives\": []\n}",
    )

    // ── arm 1: the mapping itself ──────────────────────────────────────────

    @Test
    fun `every error payload exits 1`() {
        for (payload in errorPayloads) {
            assertEquals(payload, 1, transcribeExitCode(payload))
        }
    }

    @Test
    fun `a text payload exits 0`() {
        for (payload in textPayloads) {
            assertEquals(payload, 0, transcribeExitCode(payload))
        }
    }

    @Test
    fun `an empty transcription without an error stays a success`() {
        // Deliberate: the recognizer reported that it COMPLETED, and "nothing
        // was heard" is a result the caller can read from `text` — unlike the
        // `no result` / ERROR_NO_MATCH / ERROR_SPEECH_TIMEOUT paths, which
        // arrive as an `error` payload (arm 1) and now exit 1. Inventing a
        // failure here would make this call unable to report silence at all.
        assertEquals(0, transcribeExitCode("""{"text": ""}"""))
        assertEquals(0, transcribeExitCode("{ \"text\": \"\" }"))
    }

    @Test
    fun `a payload that is not json exits 1`() {
        // Fail-closed. The only payloads this function ever sees come from
        // `recognize`, which always returns JSON, so anything else is a defect
        // that must surface rather than be reported as success.
        for (payload in listOf("", "   ", "no result", "{", "[1,2]", "{\"error\"")) {
            assertEquals(payload, 1, transcribeExitCode(payload))
        }
    }

    @Test
    fun `the error key decides, not the text beside it`() {
        // A payload carrying both keys is a failure — the error branch wins,
        // because that is what `recognize` means by including it.
        assertEquals(1, transcribeExitCode("""{"error": "No speech recognized", "text": "noise"}"""))
        // …and the word "error" inside the recognised text is not an error key.
        assertEquals(0, transcribeExitCode("""{"text": "the word error appears here"}"""))
    }

    // ── arm 2: production wiring (source shape) ────────────────────────────

    @Test
    fun `cmdTranscribe derives the exit code and forwards the payload unchanged`() {
        val body = declaration(codeOnly(readRepoFile(SPEECH)), "private fun cmdTranscribe(")
        assertTrue(
            "the payload must reach the caller exactly as recognize produced it:\n$body",
            body.contains("OffloadOutput.formatBody(text, args)"),
        )
        assertTrue(
            "the exit code must be derived from that payload:\n$body",
            body.contains(
                "NativeOffloadResult(transcribeExitCode(text), OffloadOutput.formatBody(text, args) + \"\\n\")",
            ),
        )
        assertFalse(
            "the unconditional exit 0 is back:\n$body",
            body.contains("NativeOffloadResult(0, OffloadOutput.formatBody(text"),
        )
        assertTrue("recognize must still be invoked:\n$body", body.contains("recognize(lang, maxResults"))
    }

    @Test
    fun `the terminal paths that were already non-zero are untouched`() {
        val code = codeOnly(readRepoFile(SPEECH))
        // An unavailable recognizer.
        assertTrue(
            "recognizer_unavailable must stay a failure",
            code.contains("\"recognizer_unavailable\"") &&
                Regex("""return NativeOffloadResult\(1, OffloadOutput\.formatBody\(body, args\) \+ "\\n"\)""")
                    .containsMatchIn(code),
        )
        // An explicit file source is a usage error, not a silent success.
        assertTrue("not_supported must stay exit 2", code.contains("\"not_supported\""))
        // The microphone permission gate returns its own result before recognize.
        assertTrue(
            "the mic gate must return before the recognizer runs",
            code.contains("ensureMicPermission(args)?.let { return it }"),
        )
    }

    // ── helpers ────────────────────────────────────────────────────────────

    /**
     * Full declaration (header + body) whose header contains [marker],
     * brace-balanced with string literals skipped. Fails closed when the marker
     * is absent — a probe that silently slices nothing would pass on an empty
     * string.
     */
    private fun declaration(src: String, marker: String): String {
        val idx = src.indexOf(marker)
        assertTrue("declaration '$marker' not found — fail-closed", idx >= 0)
        val open = src.indexOf("{", idx)
        assertTrue("declaration '$marker' has no body — fail-closed", open >= 0)
        var depth = 0
        var inString = false
        var i = open
        while (i < src.length) {
            val c = src[i]
            if (inString) {
                if (c == '\\') {
                    i += 2
                    continue
                }
                if (c == '"') inString = false
                i++
                continue
            }
            when (c) {
                '"' -> inString = true
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(idx, i + 1)
                }
            }
            i++
        }
        throw AssertionError("declaration '$marker' is unbalanced — fail-closed")
    }

    /**
     * Line-based comment stripper, so a comment that merely MENTIONS the exit
     * shape cannot satisfy (or break) an assertion. Safe for this file: it
     * contains no `//` inside a string literal.
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder()
        var inBlock = false
        for (line in src.lines()) {
            var s = line
            if (inBlock) {
                val end = s.indexOf("*/")
                if (end < 0) {
                    out.append('\n')
                    continue
                }
                s = s.substring(end + 2)
                inBlock = false
            }
            val d = s.indexOf("//")
            val b = s.indexOf("/*")
            s = when {
                b >= 0 && (d < 0 || b < d) -> {
                    inBlock = true
                    s.substring(0, b)
                }
                d >= 0 -> s.substring(0, d)
                else -> s
            }
            out.append(s).append('\n')
        }
        return out.toString()
    }

    private fun readRepoFile(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException("$relative not found from user.dir=${System.getProperty("user.dir")}")
    }
}
