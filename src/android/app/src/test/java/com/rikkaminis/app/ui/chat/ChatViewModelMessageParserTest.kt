package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.db.MessageEntity
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for [ChatViewModelMessageParser] — the partsJson → [ParsedPart]
 * parser that both the UI renderer and the LLM-history builder consume.
 *
 * Pure org.json logic, no Android dependencies (Room's [MessageEntity] is a
 * plain data class constructible on the JVM).
 */
class ChatViewModelMessageParserTest {

    private fun partJson(type: String, value: JSONObject? = null): JSONObject =
        JSONObject().apply {
            put("type", type)
            if (value != null) put("value", value)
        }

    /** Text parts carry the value as a plain string, not a nested object. */
    private fun textPart(value: String? = null): JSONObject =
        JSONObject().apply {
            put("type", "text")
            if (value != null) put("value", value)
        }

    // ── tryParsePartsJson / parsePartsJson ──────────────────────────────────

    @Test fun `blank partsJson returns empty list`() {
        assertEquals(emptyList<ParsedPart>(), tryParsePartsJson(""))
        assertEquals(emptyList<ParsedPart>(), tryParsePartsJson("   "))
    }

    @Test fun `empty json array returns empty list`() {
        assertEquals(emptyList<ParsedPart>(), tryParsePartsJson("[]"))
    }

    @Test fun `text part is parsed`() {
        val json = JSONArray().put(textPart("hello"))
        val parts = tryParsePartsJson(json.toString())!!
        assertEquals(1, parts.size)
        assertEquals(ParsedPart.Text("hello"), parts[0])
    }

    @Test fun `text part with missing value defaults to empty`() {
        val json = JSONArray().put(textPart())
        assertEquals(listOf(ParsedPart.Text("")), tryParsePartsJson(json.toString()))
    }

    @Test fun `toolUse part is parsed with all fields`() {
        val v = JSONObject()
            .put("toolUseId", "call_1")
            .put("name", "shell_execute")
            .put("input", "{\"cmd\":\"ls\"}")
            .put("description", "run a command")
            .put("pageURL", "http://example.com")
            .put("imageFilePath", "/tmp/shot.png")
        val json = JSONArray().put(partJson("toolUse", v))
        assertEquals(
            listOf(
                ParsedPart.ToolUse(
                    id = "call_1",
                    name = "shell_execute",
                    input = "{\"cmd\":\"ls\"}",
                    description = "run a command",
                    pageURL = "http://example.com",
                    imageFilePath = "/tmp/shot.png",
                ),
            ),
            tryParsePartsJson(json.toString()),
        )
    }

    @Test fun `toolUse empty pageURL and imageFilePath become null`() {
        val v = JSONObject()
            .put("toolUseId", "call_1")
            .put("name", "shell_execute")
            .put("input", "{}")
            .put("description", "")
            .put("pageURL", "")
            .put("imageFilePath", "")
        val json = JSONArray().put(partJson("toolUse", v))
        val parts = tryParsePartsJson(json.toString())!!
        val toolUse = parts[0] as ParsedPart.ToolUse
        assertNull(toolUse.pageURL)
        assertNull(toolUse.imageFilePath)
    }

    @Test fun `toolUse missing input defaults to empty object`() {
        val v = JSONObject()
            .put("toolUseId", "call_1")
            .put("name", "file_read")
        val json = JSONArray().put(partJson("toolUse", v))
        val parts = tryParsePartsJson(json.toString())!!
        assertEquals("{}", (parts[0] as ParsedPart.ToolUse).input)
    }

    @Test fun `toolResult success true is parsed`() {
        val v = JSONObject()
            .put("toolUseId", "call_1")
            .put("name", "shell_execute")
            .put("output", "ok")
            .put("success", true)
        val json = JSONArray().put(partJson("toolResult", v))
        assertEquals(
            listOf(ParsedPart.ToolResult("call_1", "shell_execute", "ok", true)),
            tryParsePartsJson(json.toString()),
        )
    }

    @Test fun `toolResult success false is parsed`() {
        val v = JSONObject()
            .put("toolUseId", "call_1")
            .put("name", "shell_execute")
            .put("output", "boom")
            .put("success", false)
        val json = JSONArray().put(partJson("toolResult", v))
        assertEquals(
            listOf(ParsedPart.ToolResult("call_1", "shell_execute", "boom", false)),
            tryParsePartsJson(json.toString()),
        )
    }

    @Test fun `mediaRef part is parsed`() {
        val v = JSONObject()
            .put("relativePath", "media/2026/08/14/x.png")
            .put("mimeType", "image/png")
            .put("originalFileName", "a.png")
            .put("linuxPath", "/data/user/0/com.rikkaminis.app/files/s.png")
        val json = JSONArray().put(partJson("mediaRef", v))
        assertEquals(
            listOf(
                ParsedPart.MediaRef(
                    relativePath = "media/2026/08/14/x.png",
                    mimeType = "image/png",
                    originalFileName = "a.png",
                    linuxPath = "/data/user/0/com.rikkaminis.app/files/s.png",
                ),
            ),
            tryParsePartsJson(json.toString()),
        )
    }

    @Test fun `mediaRef without value object is skipped`() {
        val json = JSONArray().put(partJson("mediaRef"))
        assertEquals(emptyList<ParsedPart>(), tryParsePartsJson(json.toString()))
    }

    @Test fun `mediaRef missing mimeType defaults to image jpeg`() {
        val v = JSONObject().put("relativePath", "media/x.png").put("originalFileName", "a.png")
        val json = JSONArray().put(partJson("mediaRef", v))
        val parts = tryParsePartsJson(json.toString())!!
        assertEquals("image/jpeg", (parts[0] as ParsedPart.MediaRef).mimeType)
    }

    @Test fun `unknown part type is skipped`() {
        val json = JSONArray().put(partJson("unknownType", JSONObject()))
        assertEquals(emptyList<ParsedPart>(), tryParsePartsJson(json.toString()))
    }

    @Test fun `mixed parts preserve order`() {
        val json = JSONArray()
            .put(textPart("first"))
            .put(partJson("toolUse", JSONObject().put("toolUseId", "c1").put("name", "file_read")))
            .put(textPart("second"))
        val parts = tryParsePartsJson(json.toString())!!
        assertEquals(3, parts.size)
        assertEquals(ParsedPart.Text("first"), parts[0])
        assertTrue(parts[1] is ParsedPart.ToolUse)
        assertEquals(ParsedPart.Text("second"), parts[2])
    }

    @Test fun `toolUse without value object makes whole parse fail`() {
        // `getJSONObject("value")` throws → entire list is rejected rather
        // than silently dropping the part (caller renders a fallback).
        val json = JSONArray().put(partJson("toolUse"))
        assertNull(tryParsePartsJson(json.toString()))
    }

    @Test fun `malformed json returns null from try variant`() {
        assertNull(tryParsePartsJson("not json at all"))
    }

    @Test fun `malformed json returns empty list from non-null variant`() {
        assertEquals(emptyList<ParsedPart>(), parsePartsJson("not json at all"))
    }

    @Test fun `non-null variant never returns null`() {
        assertEquals(emptyList<ParsedPart>(), parsePartsJson(""))
        assertEquals(emptyList<ParsedPart>(), parsePartsJson("[]"))
    }

    // ── parseRows ───────────────────────────────────────────────────────────

    private fun entity(id: String, partsJson: String) = MessageEntity(
        id = id,
        sessionId = "s1",
        role = "user",
        partsJson = partsJson,
        createdAt = 1_000L,
        sortOrder = 0,
    )

    @Test fun `parseRows empty input returns empty list`() {
        assertEquals(emptyList<ParsedRow>(), parseRows(emptyList()))
    }

    @Test fun `parseRows parses each entity once with correct metadata`() {
        val good = JSONArray().put(textPart("hi"))
        val rows = parseRows(
            listOf(
                entity("m1", good.toString()),
                entity("m2", "broken json"),
                entity("m3", ""),
            ),
        )
        assertEquals(3, rows.size)

        assertEquals("m1", rows[0].entity.id)
        assertEquals(listOf(ParsedPart.Text("hi")), rows[0].parts)
        assertEquals(good.toString().length, rows[0].sourceChars)
        assertEquals(false, rows[0].malformed)

        assertEquals("m2", rows[1].entity.id)
        assertEquals(emptyList<ParsedPart>(), rows[1].parts)
        assertEquals(true, rows[1].malformed)

        assertEquals("m3", rows[2].entity.id)
        assertEquals(emptyList<ParsedPart>(), rows[2].parts)
        assertEquals(false, rows[2].malformed)
    }

    // ── [T-think-tag-orphan-close] persisted-text cleanup ──────────────────

    private fun roleEntity(id: String, role: String, partsJson: String) = MessageEntity(
        id = id,
        sessionId = "s1",
        role = role,
        partsJson = partsJson,
        createdAt = 1_000L,
        sortOrder = 0,
    )

    @Test fun `parseRows drops orphan think closers from assistant rows`() {
        val partsJson = JSONArray().put(textPart("**final audit**</thinking>")).toString()
        val rows = parseRows(listOf(roleEntity("m1", "assistant", partsJson)))
        assertEquals(listOf(ParsedPart.Text("**final audit**")), rows[0].parts)
    }

    @Test fun `parseRows leaves non-assistant rows byte-identical`() {
        // A user row carries tool output, where a quoted token is evidence.
        val quoted = "**final audit**</thinking>"
        val partsJson = JSONArray().put(textPart(quoted)).toString()
        val rows = parseRows(listOf(roleEntity("m1", "user", partsJson)))
        assertEquals(listOf(ParsedPart.Text(quoted)), rows[0].parts)
    }

    @Test fun `parseRows keeps deliberate quotations in assistant rows`() {
        val quoted = "the gateway leaves a bare `</thinking>` behind"
        val partsJson = JSONArray().put(textPart(quoted)).toString()
        val rows = parseRows(listOf(roleEntity("m1", "assistant", partsJson)))
        assertEquals(listOf(ParsedPart.Text(quoted)), rows[0].parts)
    }

    @Test fun `parseRows keeps tool and media parts untouched`() {
        val partsJson = JSONArray()
            .put(textPart("**audit**</thinking>"))
            .put(partJson("toolUse", JSONObject().put("toolUseId", "t1").put("name", "shell")))
            .toString()
        val rows = parseRows(listOf(roleEntity("m1", "assistant", partsJson)))
        assertEquals(2, rows[0].parts.size)
        assertEquals(ParsedPart.Text("**audit**"), rows[0].parts[0])
        assertTrue(rows[0].parts[1] is ParsedPart.ToolUse)
    }
}
