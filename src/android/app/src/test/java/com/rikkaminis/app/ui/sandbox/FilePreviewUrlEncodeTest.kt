package com.rikkaminis.app.ui.sandbox

import com.rikkaminis.app.ui.chat.decodeUriPath
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * deep-audit-1022 FIX-H / backlog §1021-V: behavior tests for
 * [encodeFileUrlPath], the pure encoder FilePreviewScreen uses to build
 * `file://` URLs. Device-verified pre-fix failure: a name containing `#`
 * pasted raw into the URL made WebView parse the `#` as a fragment start,
 * so `a#b.html` loaded as `a.html` → `net::ERR_FILE_NOT_FOUND` (probe recipe
 * in shared/work/verify-1021/, VERIFY-DEVICE-1021.md §ISSUE-07).
 *
 * All expected values are hard literals — never derived from the encoder.
 * The round-trip arm pairs [encodeFileUrlPath] with the existing decoder
 * [decodeUriPath] (ui/chat/UriPathDecode.kt), the same decoder WebView's
 * percent-decode semantics mirror for path components.
 */
class FilePreviewUrlEncodeTest {

    @Test
    fun `hash in name is percent-encoded`() {
        assertEquals("/data/x/a%23b.html", encodeFileUrlPath("/data/x/a#b.html"))
    }

    @Test
    fun `question mark in name is percent-encoded`() {
        assertEquals("/data/x/q%3Fr.html", encodeFileUrlPath("/data/x/q?r.html"))
    }

    @Test
    fun `space in name is percent-encoded`() {
        assertEquals("/data/x/my%20file.html", encodeFileUrlPath("/data/x/my file.html"))
    }

    @Test
    fun `cjk name is percent-encoded as utf-8`() {
        assertEquals(
            "/data/x/%E6%88%91%E7%9A%84%20%E6%8A%A5%E5%91%8A.html",
            encodeFileUrlPath("/data/x/我的 报告.html"),
        )
    }

    @Test
    fun `literal percent is escaped`() {
        assertEquals("/data/x/50%25off.html", encodeFileUrlPath("/data/x/50%off.html"))
        // A name that already *looks* percent-encoded must still round-trip
        // to itself: leaving the literal `%23` as-is would decode to `#`.
        assertEquals("/data/x/50%2523.html", encodeFileUrlPath("/data/x/50%23.html"))
    }

    @Test
    fun `slash survives byte-for-byte`() {
        assertEquals("/a/b/c.html", encodeFileUrlPath("/a/b/c.html"))
    }

    @Test
    fun `ascii unreserved path is unchanged and idempotent`() {
        val path = "/data/x/page-1.2_3~x.html"
        val once = encodeFileUrlPath(path)
        assertEquals(path, once)
        // Idempotent on the safe class: the encoded output re-encodes to itself.
        assertEquals(once, encodeFileUrlPath(once))
    }

    @Test
    fun `full file url for a hash name`() {
        assertEquals(
            "file:///data/user/0/app/a%23b.html",
            "file://" + encodeFileUrlPath("/data/user/0/app/a#b.html"),
        )
    }

    @Test
    fun `encoded paths round-trip through decodeUriPath`() {
        val paths = listOf(
            "/data/x/a#b.html",
            "/data/x/q?r.html",
            "/data/x/my file.html",
            "/data/x/我的 报告.html",
            "/data/x/50%off.html",
            "/data/x/50%23.html",
        )
        for (p in paths) {
            assertEquals(p, decodeUriPath(encodeFileUrlPath(p)))
        }
    }
}
