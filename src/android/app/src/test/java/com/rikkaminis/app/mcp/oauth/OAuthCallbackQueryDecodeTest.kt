package com.rikkaminis.app.mcp.oauth

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1018-FIX-G] ISSUE-11: the OAuth callback used to read `uri.query` — which
 * the java.net.URI getter has already %-decoded once — and then ran
 * URLDecoder.decode a SECOND time. Observable consequence (java.net ground
 * truth, see probe/QueryDecodeGroundTruth.java): "%2B" in an authorization
 * code decoded to a space ("abc%2Bdef" -> "abc def") and "%26" was split into
 * a separate param ("code=abc%26def" -> code="abc", def=""), both of which
 * break the token exchange.
 *
 * Every expectation below is a literal written from the observed java.net
 * ground truth, never derived from the implementation (self-reference trap).
 * The function under test is the pure [parseCallbackQuery] extracted from
 * OAuthCallbackServer — the accept loop itself is exercised by
 * OAuthCallbackServerFdTest.
 */
class OAuthCallbackQueryDecodeTest {

    // 1) Task case 1: a %2B in the code must survive as a plus, not become a space.
    @Test
    fun `percent-encoded plus in code survives as plus`() {
        val params = parseCallbackQuery("code=abc%2Bdef")
        assertEquals(mapOf("code" to "abc+def"), params)
    }

    // 2) Task case 2: "+ semantics" — both "%2B" and a literal "+" yield "+".
    //    Rationale (matches the WebDavClient.decodeHrefSegment precedent): an
    //    OAuth redirect query is not a form-urlencoded body, a provider that
    //    means a space encodes "%20", and a provider that means a plus sends
    //    either "+" or "%2B" — both must arrive intact.
    @Test
    fun `literal plus and percent-encoded plus both stay plus`() {
        assertEquals("a+b", parseCallbackQuery("code=a%2Bb")["code"])
        assertEquals("a+b", parseCallbackQuery("code=a+b")["code"])
    }

    // 3) Task case 3: "%26" must not split the value into two params (the
    //    double-decode path produced {code=abc, def=} here).
    @Test
    fun `percent-encoded ampersand does not split the param`() {
        val params = parseCallbackQuery("code=abc%26def&state=s1")
        assertEquals(2, params.size)
        assertEquals("abc&def", params["code"])
        assertEquals("s1", params["state"])
    }

    // 4) Task case 4: a value containing '=' is kept whole (split limit 2).
    @Test
    fun `value containing equals is kept whole`() {
        val params = parseCallbackQuery("code=x&state=a=b")
        assertEquals("x", params["code"])
        assertEquals("a=b", params["state"])
    }

    // 5) Task case 5: parse order does not matter and the map is complete.
    @Test
    fun `multi-param parse is order independent`() {
        val a = parseCallbackQuery("state=s1&code=c1")
        val b = parseCallbackQuery("code=c1&state=s1")
        assertEquals(a, b)
        assertEquals(mapOf("code" to "c1", "state" to "s1"), a)
    }

    // 6) Task case 6: empty / bare / malformed queries must not throw.
    @Test
    fun `empty and malformed queries do not throw`() {
        assertEquals(emptyMap<String, String>(), parseCallbackQuery(null))
        assertEquals(emptyMap<String, String>(), parseCallbackQuery(""))
        assertEquals(emptyMap<String, String>(), parseCallbackQuery("&"))
        // bare key without '=' -> empty string value (previous behavior kept).
        assertEquals(mapOf("code" to ""), parseCallbackQuery("code"))
        // undecodable escape sequence -> that pair is skipped, others survive.
        assertEquals(mapOf("state" to "s"), parseCallbackQuery("code=ab%2&state=s"))
        // empty segments are skipped, not mapped to a spurious "" key.
        assertEquals(mapOf("code" to "c1", "state" to "s1"), parseCallbackQuery("&code=c1&&state=s1&"))
    }

    // 7) Bonus: a real space still decodes to a space — both old and new paths
    //    agree here, locking the boundary of the change.
    @Test
    fun `percent-encoded space still decodes to space`() {
        assertEquals("a b", parseCallbackQuery("code=a%20b")["code"])
        assertTrue(parseCallbackQuery("code=a%20b").size == 1)
    }
}
