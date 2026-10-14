package com.rikkaminis.app.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/bundled-skill-asset-hash-gate] Pure-JVM tests for the bundled-sibling
 * extraction gate.
 *
 * Regression background (2026-10-14, device-verified): `installBundledSkill`
 * gated the entire install — including the sibling extraction — on the SKILL.md
 * `version`. FIX-RV-C2's zero-count guard was therefore present in
 * `assets/skills/semantic-memory/semantic_memory.py` inside the APK while the
 * live file on the device did not have it (both sides at `version: 1.4.1`).
 *
 * These tests pin the two properties the fix depends on:
 *  - the hash is *stable* for an unchanged payload (otherwise every launch
 *    would rewrite every extracted script, discarding user edits), and
 *  - the hash is *sensitive* to any content, path or layout change (otherwise
 *    a changed script would ship and never land).
 *
 * Expected values are literals observed from a probe run, not derived from the
 * implementation — deriving them would let a mutation of the layout silently
 * keep the test green.
 */
class SkillAssetSyncTest {

    private fun e(path: String, content: String) = SkillAssetSync.Entry(path, content.toByteArray(Charsets.UTF_8))

    private val fixture = listOf(e("b.py", "beta"), e("scripts/a.py", "alpha"))

    @Test
    fun payloadHashIsStableForUnchangedPayload() {
        // Same payload, different list order (AssetManager.list order is not
        // guaranteed) must hash identically.
        assertEquals(SkillAssetSync.payloadHash(fixture), SkillAssetSync.payloadHash(fixture.reversed()))
        assertEquals(
            "e4c6213622b9910bc34cebc7ff23edf60f2a8d3c3ce122bb57b6d81be7ebf375",
            SkillAssetSync.payloadHash(fixture),
        )
    }

    @Test
    fun emptyPayloadHashesAsSha256OfNothing() {
        // Independent ground truth: SHA-256 of the empty input.
        assertEquals(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            SkillAssetSync.payloadHash(emptyList()),
        )
    }

    @Test
    fun payloadHashDetectsRenamedContent() {
        // Swapping the two files' contents must change the hash even though the
        // multiset of bytes is identical — paths are part of the digest.
        val swapped = listOf(e("scripts/a.py", "beta"), e("b.py", "alpha"))
        assertEquals(
            "4f059c601746255124963b4eddc6a4a4cd3e8b07d8c93c5427bf36008f991372",
            SkillAssetSync.payloadHash(swapped),
        )
        assertNotEquals(SkillAssetSync.payloadHash(fixture), SkillAssetSync.payloadHash(swapped))
    }

    @Test
    fun payloadHashIsLengthDelimited() {
        // ("ab","c") and ("a","bc") concatenate to the same bytes — the digest
        // must still separate them (length prefixes).
        val shuffle = SkillAssetSync.payloadHash(listOf(e("ab", "c"), e("x", "")))
        val split = SkillAssetSync.payloadHash(listOf(e("a", "bc"), e("x", "")))
        assertEquals("9faa216e0b88a4b779730adf3408d8d67a14968e93f441d5c4cc27e9db59585a", shuffle)
        assertEquals("5bfc1d2e2713039176823f537285c0201250beba49dd25cb1d5610ca8644263c", split)
    }

    @Test
    fun needsExtractionOnlyWhenHashDiffers() {
        assertTrue("no stored hash (first install / pre-manifest build)", SkillAssetSync.needsExtraction(null, "abc"))
        assertFalse("payload unchanged — user edits to a script survive", SkillAssetSync.needsExtraction("abc", "abc"))
        assertTrue("payload changed in the new APK", SkillAssetSync.needsExtraction("abc", "def"))
    }

    @Test
    fun manifestRoundTrips() {
        val hashes = mapOf(
            "semantic-memory" to "aa11",
            "github-ops" to "bb22",
            "cloudflare-fullright-ops" to "cc33",
        )
        val text = SkillAssetSync.renderManifest(hashes)
        assertEquals(hashes, SkillAssetSync.parseManifest(text))
    }

    @Test
    fun renderManifestIsSortedAndNewlineTerminated() {
        val text = SkillAssetSync.renderManifest(mapOf("zeta" to "zz", "alpha" to "aa"))
        assertEquals("alpha=aa\nzeta=zz\n", text)
    }

    @Test
    fun parseManifestDropsMalformedLines() {
        // A partially written manifest must not invent entries; a dropped one
        // only costs one idempotent re-extraction. First '=' splits the line.
        val parsed = SkillAssetSync.parseManifest(
            "alpha=aa\n" +
                "\n" +
                "   \n" +
                "garbage-without-separator\n" +
                "=novalue\n" +
                "beta=bb=cc\n" +
                "  delta = dd  \n" +
                "zeta=zz\n",
        )
        assertEquals(
            mapOf("alpha" to "aa", "beta" to "bb=cc", "delta" to "dd", "zeta" to "zz"),
            parsed,
        )
    }

    @Test
    fun entryEqualityComparesBytes() {
        // data class + ByteArray defaults to identity comparison — a footgun
        // that would make every payload look "changed".
        assertEquals(e("a.py", "same"), e("a.py", "same"))
        assertNotEquals(e("a.py", "same"), e("a.py", "different"))
        assertNotEquals(e("a.py", "same"), e("b.py", "same"))
    }
}
