package com.rikkaminis.app.providers

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1034-A2 ISSUE-02] Pins the two id decisions that used to read the RAW
 * document id.
 *
 * DocumentsContract does not normalize client ids: the Files app and plenty of
 * third-party clients legitimately address the root as "", " ", "/" or "//".
 * The old `parentDocumentId.isEmpty()` treated every non-empty spelling as a
 * non-root parent, skipped the TOP_LEVEL whitelist and leaked internal
 * filesDir entries into the listing; `deleteDocument("shared")` deleted the
 * whole shared tree because nothing distinguished a top-level entry from its
 * children.
 *
 * Expectations are HARD-CODED literals (no derivation from the helpers under
 * test) so a future regression cannot drag the expectations along with it —
 * the 2026-09-26 self-referential-fixture lesson.
 */
class MinisDocumentsProviderIdTest {

    // ── isRootDocId: every legitimate spelling of "the root" ─────────────

    @Test
    fun rootSpelling_empty() {
        assertTrue(MinisDocumentsProvider.isRootDocId(""))
    }

    @Test
    fun rootSpelling_blank() {
        assertTrue(MinisDocumentsProvider.isRootDocId(" "))
    }

    @Test
    fun rootSpelling_singleSlash() {
        // The regression this branch fixes: "/" was treated as a non-root
        // parent, so the TOP_LEVEL filter was skipped for it.
        assertTrue(MinisDocumentsProvider.isRootDocId("/"))
    }

    @Test
    fun rootSpelling_doubleSlash() {
        assertTrue(MinisDocumentsProvider.isRootDocId("//"))
    }

    @Test
    fun rootSpelling_slashAndSpaces() {
        assertTrue(MinisDocumentsProvider.isRootDocId(" / "))
    }

    @Test
    fun nonRoot_topLevelEntry() {
        assertFalse(MinisDocumentsProvider.isRootDocId("shared"))
    }

    @Test
    fun nonRoot_child() {
        assertFalse(MinisDocumentsProvider.isRootDocId("shared/notes/a.txt"))
    }

    @Test
    fun nonRoot_leadingSlashIsStillAnEntry() {
        assertFalse(MinisDocumentsProvider.isRootDocId("/memory"))
    }

    // ── isTopLevelDocId: the entry ITSELF, not something inside it ──────

    @Test
    fun topLevel_shared() {
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("shared"))
    }

    @Test
    fun topLevel_memory() {
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("memory"))
    }

    @Test
    fun topLevel_skills() {
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("skills"))
    }

    @Test
    fun topLevel_leadingSlashNormalizesAway() {
        // A client that addresses the shared node as "/shared" is still asking
        // about the top-level entry itself.
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("/shared"))
    }

    @Test
    fun topLevel_childIsNotTopLevel() {
        assertFalse(MinisDocumentsProvider.isTopLevelDocId("shared/notes"))
    }

    @Test
    fun topLevel_deepChildIsNotTopLevel() {
        assertFalse(MinisDocumentsProvider.isTopLevelDocId("memory/2026/10/a.md"))
    }

    @Test
    fun topLevel_rootIsNotTopLevel() {
        // "" (and "/", " ") name the ROOT, which is not a deletable entry.
        assertFalse(MinisDocumentsProvider.isTopLevelDocId(""))
    }

    @Test
    fun topLevel_rootSlashIsNotTopLevel() {
        assertFalse(MinisDocumentsProvider.isTopLevelDocId("/"))
    }

    // ── the decisions deleteDocument/renameDocument actually make ────────

    @Test
    fun deleteDecision_topLevelSharedMustBeRefused() {
        // `deleteDocument("shared")` used to deleteRecursively() the whole
        // shared tree; the new guard refuses exactly this shape.
        val documentId = "shared"
        val refuseDelete = MinisDocumentsProvider.isRootDocId(documentId) ||
            MinisDocumentsProvider.isTopLevelDocId(documentId)
        assertTrue(refuseDelete)
    }

    @Test
    fun deleteDecision_childOfSharedIsDeletable() {
        val documentId = "shared/notes/stale.md"
        val refuseDelete = MinisDocumentsProvider.isRootDocId(documentId) ||
            MinisDocumentsProvider.isTopLevelDocId(documentId)
        assertFalse(refuseDelete)
    }

    @Test
    fun deleteDecision_readOnlyTopStaysReadOnlyTop() {
        // unchanged contract: memory/skills are read-only regardless of this
        // branch (readOnlyTopOf covers them); the top-level guard must ALSO
        // fire for them, and it does via isTopLevelDocId.
        assertEquals("memory", MinisDocumentsProvider.readOnlyTopOf("memory"))
        assertEquals("skills", MinisDocumentsProvider.readOnlyTopOf("skills/subdir"))
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("memory"))
        assertTrue(MinisDocumentsProvider.isTopLevelDocId("skills"))
    }
}
