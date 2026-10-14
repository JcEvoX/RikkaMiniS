package com.rikkaminis.app.providers

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix-R12 D7] Pure id-normalization / read-only mapping / canonical prefix
 * guard for MinisDocumentsProvider, tested without a Provider context.
 *
 * The defect being pinned: the raw `documentId.substringBefore('/')` judged
 * read-only on the UN-normalized id, so "/memory" (leading slash) had top ""
 * and slipped past READ_ONLY_TOP — and `deleteDocument("")` deleted the whole
 * provider root. Call sites must pair normalizeDocId + readOnlyTopOf.
 */
class MinisDocumentsProviderPathTest {

    // --- normalizeDocId: the defect table ---
    @Test
    fun normalize_leadingAndTrailingSlashes() {
        assertEquals("memory", MinisDocumentsProvider.normalizeDocId("/memory"))
        assertEquals("memory/GLOBAL.md", MinisDocumentsProvider.normalizeDocId("/memory/GLOBAL.md/"))
        assertEquals("memory/x", MinisDocumentsProvider.normalizeDocId("//memory/x"))
        assertEquals("memory/x", MinisDocumentsProvider.normalizeDocId(" memory/x "))
        assertEquals("", MinisDocumentsProvider.normalizeDocId(""))
        assertEquals("", MinisDocumentsProvider.normalizeDocId("/"))
    }

    // --- readOnlyTopOf on the NORMALIZED id (the actual fix) ---
    @Test
    fun readOnlyTop_normalizedIdsMapToReadOnlyTops() {
        assertEquals(
            "memory",
            MinisDocumentsProvider.readOnlyTopOf(MinisDocumentsProvider.normalizeDocId("/memory")),
        )
        assertEquals("memory", MinisDocumentsProvider.readOnlyTopOf("memory/GLOBAL.md"))
        assertEquals(
            "skills",
            MinisDocumentsProvider.readOnlyTopOf(MinisDocumentsProvider.normalizeDocId("/skills/")),
        )
        assertNull(MinisDocumentsProvider.readOnlyTopOf("shared/notes.txt"))
        // Root itself: not a read-only top; emptiness is refused explicitly at call sites.
        assertNull(MinisDocumentsProvider.readOnlyTopOf(""))
    }

    // The old raw substringBefore('/') returned "" for "/memory" — which is
    // neither null-for-writable-nor-matched. This pins that the pair
    // (normalize + readOnlyTop) is exactly what call sites use.
    @Test
    fun readOnlyTop_leadingSlashNoLongerEscapes() {
        val clean = MinisDocumentsProvider.normalizeDocId("/memory")
        assertTrue(clean.isNotEmpty())
        assertEquals("memory", MinisDocumentsProvider.readOnlyTopOf(clean))
    }

    // --- isWithinRoot canonical prefix guard ---
    @Test
    fun isWithinRoot_exactRootAllowed() {
        assertTrue(MinisDocumentsProvider.isWithinRoot("/data/minis-global", "/data/minis-global"))
    }

    @Test
    fun isWithinRoot_childAllowed() {
        assertTrue(
            MinisDocumentsProvider.isWithinRoot(
                "/data/minis-global",
                "/data/minis-global/memory/GLOBAL.md",
            ),
        )
    }

    @Test
    fun isWithinRoot_prefixSiblingRejected() {
        // The exact shape the missing File.separator allowed: "shared_evil" is
        // NOT under "shared", yet raw startsWith("/data/shared") passed it.
        assertFalse(MinisDocumentsProvider.isWithinRoot("/data/shared", "/data/shared_evil/x"))
        assertFalse(
            MinisDocumentsProvider.isWithinRoot("/data/minis-global", "/data/minis-global-evil/x"),
        )
    }

    @Test
    fun isWithinRoot_outsideRejected() {
        assertFalse(
            MinisDocumentsProvider.isWithinRoot("/data/minis-global", "/data/data/com.other/files"),
        )
        assertFalse(MinisDocumentsProvider.isWithinRoot("/data/minis-global", "/etc/passwd"))
    }
}
