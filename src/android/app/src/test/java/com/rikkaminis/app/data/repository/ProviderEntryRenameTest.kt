package com.rikkaminis.app.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1022-E] JVM tests for [decideEntryRename] — the decision core of
 * ProviderRepository.updateEntry's id-rename path (deep-audit-1022 ISSUE-05).
 *
 * ProviderRepository itself needs a Room-backed Context and is not
 * JVM-constructible, so the rename decision was extracted into
 * ProviderRepositoryUtils (house pattern: pure functions live there and get
 * JVM-tested directly). updateEntry's application of the decision —
 * uuid canonicalization, group/agentLoop reference sync and the
 * KEY_LAST_USED_ENTRY prefs rewrite — is pinned by
 * scripts/scan/entry_id_rename_integrity_check.py (arm 1), which reads the
 * real wiring anchors.
 *
 * Expected values are hardcoded literals, NOT derived from production
 * constants (evidence-discipline: a mutation must not move the expectation).
 */
class ProviderEntryRenameTest {

    private val others = listOf(
        "inst-1" to "gpt-4o",
        "inst-1" to "claude-3",
        "inst-2" to "gpt-4o",
    )

    // ── fail-closed: blank ids ─────────────────────────────────────────────

    @Test fun `blank model id is rejected with reason`() {
        val d = decideEntryRename(
            existingUuid = "inst-1/my-model",
            updatedInstanceId = "inst-1",
            updatedModelId = "",
            others = others,
        )
        assertFalse(d.allowed)
        assertEquals("blank model id", d.rejectionReason)
        assertNull(d.oldKey)
        assertNull(d.newKey)
    }

    @Test fun `whitespace-only model id is rejected`() {
        val d = decideEntryRename(
            existingUuid = "inst-1/my-model",
            updatedInstanceId = "inst-1",
            updatedModelId = "   ",
            others = others,
        )
        assertFalse(d.allowed)
        assertEquals("blank model id", d.rejectionReason)
    }

    // ── no rename: composite key unchanged ──────────────────────────────────

    @Test fun `unchanged composite key is allowed without rename`() {
        val d = decideEntryRename(
            existingUuid = "inst-1/gpt-4o",
            updatedInstanceId = "inst-1",
            updatedModelId = "gpt-4o",
            others = others,
        )
        assertTrue(d.allowed)
        assertNull(d.rejectionReason)
        assertNull(d.oldKey)
        assertNull(d.newKey)
    }

    @Test fun `overrides-only update of built-in entry carries no rename`() {
        // Real call shape from ModelGroupDetailScreen / ManageProviderModelsSheet:
        // same id, same instance — only costTier/visibility change.
        val d = decideEntryRename(
            existingUuid = "inst-1/claude-3",
            updatedInstanceId = "inst-1",
            updatedModelId = "claude-3",
            others = others,
        )
        assertTrue(d.allowed)
        assertNull(d.newKey)
    }

    // ── rename: remap keys are minted from the new composite key ────────────

    @Test fun `rename without collision carries old and new composite keys`() {
        val d = decideEntryRename(
            existingUuid = "inst-1/old-name",
            updatedInstanceId = "inst-1",
            updatedModelId = "new-name",
            others = others,
        )
        assertTrue(d.allowed)
        assertNull(d.rejectionReason)
        assertEquals("inst-1/old-name", d.oldKey)
        assertEquals("inst-1/new-name", d.newKey)
    }

    // ── fail-closed: colliding renames ─────────────────────────────────────

    @Test fun `rename colliding with another entry of same instance is rejected`() {
        val d = decideEntryRename(
            existingUuid = "inst-1/old-name",
            updatedInstanceId = "inst-1",
            updatedModelId = "gpt-4o",
            others = others,
        )
        assertFalse(d.allowed)
        // Room upsertEntries would REPLACE the existing inst-1/gpt-4o row.
        assertTrue(d.rejectionReason!!.contains("inst-1/gpt-4o"))
        assertNull(d.oldKey)
        assertNull(d.newKey)
    }

    @Test fun `same model id under another instance is not a collision`() {
        // Colliding keys are per (instance, model); the composite prefix keeps
        // the same model id addressable under different providers. "claude-3"
        // exists under inst-1 in `others` — renaming OUR entry to
        // inst-2/claude-3 must NOT read that as a collision. (A version of
        // this test that picked inst-2/gpt-4o — which DOES exist in others —
        // failed here first: the fixture itself was wrong, the harness tooth
        // works.)
        val d = decideEntryRename(
            existingUuid = "inst-1/old-name",
            updatedInstanceId = "inst-2",
            updatedModelId = "claude-3",
            others = others,
        )
        assertTrue(d.allowed)
        assertEquals("inst-1/old-name", d.oldKey)
        assertEquals("inst-2/claude-3", d.newKey)
    }

    @Test fun `collision check ignores the entry being renamed`() {
        // The renamed entry itself already occupies (inst-1, claude-3); a
        // no-op-style decision must not read that as a collision.
        val d = decideEntryRename(
            existingUuid = "inst-1/claude-3",
            updatedInstanceId = "inst-1",
            updatedModelId = "claude-3",
            others = others,
        )
        assertTrue(d.allowed)
        assertNull(d.newKey)
    }

    // ── boundary: composite key format is instanceId + "/" + modelId ───────

    @Test fun `new key keeps the literal composite shape`() {
        val d = decideEntryRename(
            existingUuid = "provider-a/legacy",
            updatedInstanceId = "provider-a",
            updatedModelId = "renamed",
            others = emptyList(),
        )
        assertEquals("provider-a/renamed", d.newKey)
    }

    @Test fun `empty others list allows any rename`() {
        val d = decideEntryRename(
            existingUuid = "solo/old",
            updatedInstanceId = "solo",
            updatedModelId = "fresh",
            others = emptyList(),
        )
        assertTrue(d.allowed)
        assertEquals("solo/fresh", d.newKey)
    }
}
