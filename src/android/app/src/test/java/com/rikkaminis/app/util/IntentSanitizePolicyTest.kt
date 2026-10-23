package com.rikkaminis.app.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1046 FIX-F9] Behavioural half of the shared `intent://`
 * hardening. `Intent` itself needs an Android runtime, so the DECISIONS live in
 * [IntentSanitizePolicy] (Android-free) and are pinned here; the thin Android
 * translation in [IntentSanitizer] is covered by
 * `BrowserExternalSchemeShapeTest` and `PhotosImportShapeTest` plus
 * `scripts/scan/offload_host_path_check.py` R2.
 */
class IntentSanitizePolicyTest {

    private val self = "com.rikkaminis.app"

    @Test
    fun `a same-package target is ours and must be stripped`() {
        assertTrue(IntentSanitizePolicy.isSelfTarget(self, self))
        // `Intent.parseUri` writes `package=` into getPackage(); component and
        // package targets both bypass exported=false for a same-uid launch.
        assertTrue(IntentSanitizePolicy.isSelfTarget("com.rikkaminis.app", self))
    }

    @Test
    fun `a third-party target survives — that is how intent links open other apps`() {
        assertFalse(IntentSanitizePolicy.isSelfTarget("com.tencent.mm", self))
        assertFalse(IntentSanitizePolicy.isSelfTarget("com.android.vending", self))
        // A sibling build / debug package is NOT us either: dropping it would be
        // an unconditional strip wearing a guard's clothes.
        assertFalse(IntentSanitizePolicy.isSelfTarget("com.rikkaminis.app.debug", self))
        assertFalse(IntentSanitizePolicy.isSelfTarget(null, self))
        assertFalse(IntentSanitizePolicy.isSelfTarget("", self))
    }

    @Test
    fun `browser_fallback_url only survives as an ordinary web url`() {
        assertEquals("https://example.com/x", IntentSanitizePolicy.allowedFallbackUrl("https://example.com/x"))
        assertEquals("http://example.com/x", IntentSanitizePolicy.allowedFallbackUrl("http://example.com/x"))
        // Scheme match is case-insensitive (RFC 3986), the payload is untouched.
        assertEquals("HTTPS://Example.com/A", IntentSanitizePolicy.allowedFallbackUrl("HTTPS://Example.com/A"))
        assertEquals("https://example.com/x", IntentSanitizePolicy.allowedFallbackUrl("  https://example.com/x  "))
    }

    @Test
    fun `a fallback that is not http(s) is dropped, not launched`() {
        // The chain this closes: `intent://…#Intent;S.browser_fallback_url=
        // intent://…` — the sanitizer strips the outer intent, then the page's
        // fallback rebuilds the very escape.
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("intent://evil#Intent;end"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("INTENT://evil#Intent;end"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("file:///data/data/com.rikkaminis.app/databases/minis.db"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("content://com.rikkaminis.app.fileprovider/shared/x"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("javascript:alert(1)"))
        // No scheme / protocol-relative: fail closed rather than guess.
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("example.com/x"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("//example.com/x"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("/relative/path"))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl(null))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl(""))
        assertNull(IntentSanitizePolicy.allowedFallbackUrl("   "))
    }

    @Test
    fun `only the scheme prefix is lower-cased, the payload is byte-identical`() {
        assertEquals(
            "intent://x#Intent;action=android.intent.action.SET_ALARM;end",
            lowercaseIntentScheme("INTENT://x#Intent;action=android.intent.action.SET_ALARM;end"),
        )
        assertEquals("android-app://com.example/Path", lowercaseIntentScheme("ANDROID-APP://com.example/Path"))
        assertEquals(
            "intent://x#Intent;S.content=Hello;end",
            lowercaseIntentScheme("intent://x#Intent;S.content=Hello;end"),
        )
        assertEquals("https://Example.com/A", lowercaseIntentScheme("HTTPS://Example.com/A"))
        assertEquals("just-a-string", lowercaseIntentScheme("just-a-string"))
        assertEquals(":odd", lowercaseIntentScheme(":odd"))
    }
}
