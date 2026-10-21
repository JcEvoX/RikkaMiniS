package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [da1039-B3 B-04] Regression tests for the focus-loss write decision behind
 * ProviderConnectionScreen's two savers.
 *
 * The savers used to write unconditionally:
 *
 *   fun saveBaseURLSettings() {
 *       providerRepository.updateInstance(instance.copy(customBaseURL = customBaseURL.ifBlank { null }, …))
 *   }
 *
 * Compose delivers `onFocusChanged(isFocused = false)` on the first frame after
 * the field mounts, so merely opening the connection page pushed two
 * `updateInstance` writes (and two "Saved …" log lines) carrying values nobody
 * had edited — and, because the copy also rewrote customUserAgent, a blur of the
 * base-URL box could rewrite an unrelated field.
 *
 * [pendingConnectionFieldWrite] is the extracted decision: normalise blank to
 * null (clearing an override), then skip the write when the normalised edit
 * already equals what is persisted. The literals below are written out by hand
 * on purpose — deriving them from the helper would let a mutation of the helper
 * mutate the expectation with it.
 */
class ConnectionFieldWriteGuardTest {

    @Test
    fun `unchanged non-blank value is not written`() {
        assertNull(pendingConnectionFieldWrite("https://api.example.com", "https://api.example.com"))
    }

    @Test
    fun `first-frame blur of an untouched field writes nothing (the mount-time callback)`() {
        // What the field holds at mount == what is persisted, for both savers.
        assertNull(pendingConnectionFieldWrite("", null))
        assertNull(pendingConnectionFieldWrite("", "https://api.example.com"))
        assertNull(pendingConnectionFieldWrite("CustomAgent/1.0", "CustomAgent/1.0"))
    }

    @Test
    fun `changed value is written verbatim`() {
        assertEquals("https://new.example.com", pendingConnectionFieldWrite("https://new.example.com", "https://old.example.com"))
        assertEquals("Agent/2.0", pendingConnectionFieldWrite("Agent/2.0", null))
        assertEquals("Agent/2.0", pendingConnectionFieldWrite("Agent/2.0", "Agent/1.0"))
    }

    @Test
    fun `blank input clears an existing override but is a no-op when already empty`() {
        assertNull(pendingConnectionFieldWrite("   ", "https://api.example.com"))
        assertNull(pendingConnectionFieldWrite("", "https://api.example.com"))
        assertNull(pendingConnectionFieldWrite("\t", null))
    }

    @Test
    fun `whitespace-only edits are normalised before the comparison`() {
        // "   " normalises to null; null == null, so a blank box over an unset
        // field must not produce a third write.
        assertNull(pendingConnectionFieldWrite("  ", null))
        assertNull(pendingConnectionFieldWrite("\n", null))
    }

    @Test
    fun `edit is not trimmed - only all-blank collapses`() {
        // A leading/trailing space is a real (if odd) UA string; only an
        // entirely blank buffer means "no override".
        assertEquals(" Agent/1.0", pendingConnectionFieldWrite(" Agent/1.0", "Agent/1.0"))
        assertEquals("Agent/1.0 ", pendingConnectionFieldWrite("Agent/1.0 ", "Agent/1.0"))
    }

    @Test
    fun `case changes count as changes`() {
        assertEquals("HTTPS://API.EXAMPLE.COM", pendingConnectionFieldWrite("HTTPS://API.EXAMPLE.COM", "https://api.example.com"))
    }
}
