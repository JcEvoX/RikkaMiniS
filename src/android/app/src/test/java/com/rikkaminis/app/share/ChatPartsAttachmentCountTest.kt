package com.rikkaminis.app.share

import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 / A-04] Regression tests for the exported attachment count.
 *
 * The defect: `ChatExporter.countAttachments` switched on the part NAME only,
 * comparing it against the pre-mediaRef names (`"image"` / `"image_url"` /
 * `"video"` / `"video_url"`). Every row persisted since the mediaRef migration
 * carries the `@SerialName` values declared in `ContentPart.kt` — a
 * `"mediaRef"` part, or a `"toolResult"` whose `value` carries a `mediaRef` —
 * so the counters were structural zeros: summed per batch, published in
 * `Summary.imageAttachments` / `videoAttachments`, written into the exported
 * `session.json`, and never incremented once.
 *
 * The expected values below are HAND-WRITTEN literals. They are never derived
 * from the implementation under test — a test whose expectation is computed
 * from the code it grades cannot fail when the code is wrong.
 */
class ChatPartsAttachmentCountTest {

    /** Build a parts array from raw JSON text. */
    private fun parts(json: String): JSONArray = JSONArray(json)

    // ── the modern serial names ─────────────────────────────────────────────

    @Test
    fun mediaRefWithImageMimeCountsAsImage() {
        val arr = parts(
            """[{"type":"mediaRef","value":{"mimeType":"image/png","id":"a","relativePath":"x.png"}}]"""
        )
        assertEquals(1 to 0, countPartsAttachments(arr))
    }

    @Test
    fun mediaRefWithVideoMimeCountsAsVideo() {
        val arr = parts(
            """[{"type":"mediaRef","value":{"mimeType":"video/mp4","id":"b","relativePath":"x.mp4"}}]"""
        )
        assertEquals(0 to 1, countPartsAttachments(arr))
    }

    @Test
    fun mediaRefWithAbsentMimeCountsAsImage() {
        // The sibling readers (ChatRepository.extractTextPreview) classify an
        // unclassifiable MediaRef as an image, not as "neither".
        val arr = parts("""[{"type":"mediaRef","value":{"id":"c","relativePath":"x.bin"}}]""")
        assertEquals(1 to 0, countPartsAttachments(arr))
    }

    @Test
    fun mediaRefWithUnknownMimeCountsAsImage() {
        val arr = parts("""[{"type":"mediaRef","value":{"mimeType":"application/octet-stream"}}]""")
        assertEquals(1 to 0, countPartsAttachments(arr))
    }

    @Test
    fun videoMimeIsMatchedCaseInsensitively() {
        val arr = parts("""[{"type":"mediaRef","value":{"mimeType":"VIDEO/MP4"}}]""")
        assertEquals(0 to 1, countPartsAttachments(arr))
    }

    @Test
    fun toolResultCarryingMediaRefCounts() {
        // A tool result that hands back media carries `value.mediaRef`.
        val arr = parts(
            """[{"type":"toolResult","value":{"toolUseId":"t1","name":"screenshot",
               "output":"ok","success":true,"mediaRef":{"mimeType":"image/png","id":"d"}}}]"""
        )
        assertEquals(1 to 0, countPartsAttachments(arr))
    }

    @Test
    fun toolResultCarryingVideoMediaRefCountsAsVideo() {
        val arr = parts(
            """[{"type":"toolResult","value":{"name":"clip","mediaRef":{"mimeType":"video/webm"}}}]"""
        )
        assertEquals(0 to 1, countPartsAttachments(arr))
    }

    @Test
    fun toolResultWithoutMediaRefCountsNothing() {
        val arr = parts(
            """[{"type":"toolResult","value":{"toolUseId":"t1","name":"read","output":"hi","success":true}}]"""
        )
        assertEquals(0 to 0, countPartsAttachments(arr))
    }

    @Test
    fun mixedPartsCountEachKind() {
        val arr = parts(
            """[{"type":"mediaRef","value":{"mimeType":"image/png"}},
                {"type":"mediaRef","value":{"mimeType":"video/mp4"}},
                {"type":"text","value":"hello"},
                {"type":"toolResult","value":{"mediaRef":{"mimeType":"image/jpeg"}}}]"""
        )
        assertEquals(2 to 1, countPartsAttachments(arr))
    }

    // ── the legacy names must keep counting ────────────────────────────────

    @Test
    fun legacyImageNamesStillCount() {
        val arr = parts(
            """[{"type":"image"},{"type":"image_url","value":{"url":"http://x"}}]"""
        )
        assertEquals(2 to 0, countPartsAttachments(arr))
    }

    @Test
    fun legacyVideoNamesStillCount() {
        val arr = parts("""[{"type":"video"},{"type":"video_url","value":{"url":"http://x"}}]""")
        assertEquals(0 to 2, countPartsAttachments(arr))
    }

    @Test
    fun legacyAndModernNamesCoexist() {
        val arr = parts(
            """[{"type":"image"},{"type":"mediaRef","value":{"mimeType":"video/mp4"}}]"""
        )
        assertEquals(1 to 1, countPartsAttachments(arr))
    }

    // ── degenerate input ────────────────────────────────────────────────────

    @Test
    fun emptyArrayCountsNothing() {
        assertEquals(0 to 0, countPartsAttachments(parts("[]")))
    }

    @Test
    fun nonObjectEntriesAreSkipped() {
        val arr = parts("""[1,"x",null,{"type":"mediaRef","value":{"mimeType":"image/png"}}]""")
        assertEquals(1 to 0, countPartsAttachments(arr))
    }

    @Test
    fun unknownPartTypesAreIgnored() {
        val arr = parts("""[{"type":"text","value":"hi"},{"type":"thinking"}]""")
        assertEquals(0 to 0, countPartsAttachments(arr))
    }

    // ── the classifier helper ───────────────────────────────────────────────

    @Test
    fun isVideoMediaRefRejectsNullAndMissingMime() {
        assertFalse(isVideoMediaRef(null))
        assertFalse(isVideoMediaRef(org.json.JSONObject("""{"id":"x"}""")))
    }

    @Test
    fun isVideoMediaRefAcceptsVideoMimeOnly() {
        assertTrue(isVideoMediaRef(org.json.JSONObject("""{"mimeType":"video/mp4"}""")))
        assertFalse(isVideoMediaRef(org.json.JSONObject("""{"mimeType":"image/png"}""")))
    }
}
