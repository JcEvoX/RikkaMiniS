package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1022-F] JVM tests for [autoSelectFastest] — the pure decision behind the
 * speed-test auto-select. Expected values are hard-coded literals (never
 * derived from the code under test) so a mutation cannot silently flip them.
 *
 * Background (deep-audit-1022 ISSUE-06): runAllTests used to record the
 * auto-selected mirror id only (no useCustomMirror flag, no applyMirror), so
 * the speed-test conclusion never reached the rootfs config. The fix delegates
 * the "which category gets which fastest result" decision to this pure
 * function; the caller performs the state writes, persistence, and the
 * applyMirror disk IO off the main thread.
 */
class MirrorAutoSelectTest {

    private fun entry(id: String, category: MirrorCategory) = MirrorEntry(
        id = id,
        name = id,
        baseURL = "https://example.invalid/$id/",
        testURL = "https://example.invalid/$id/probe",
        category = category,
        region = "Test",
    )

    private fun result(id: String, category: MirrorCategory, latencyMs: Int?) =
        MirrorTestResult(entry(id, category), latencyMs, if (latencyMs == null) "HTTP 500" else null)

    @Test
    fun picksFirstSuccessfulEntryOfSortedInput() {
        // Contract: the caller feeds results ordered by the ViewModel's
        // latencyComparator (successes first, ascending latency; failures
        // last) — see runAllTests. The decision therefore takes the first
        // successful entry; it does not re-sort.
        val sorted = mapOf(
            MirrorCategory.ALPINE to listOf(
                result("alpine.fast", MirrorCategory.ALPINE, 120),
                result("alpine.slow", MirrorCategory.ALPINE, 400),
                result("alpine.dead", MirrorCategory.ALPINE, null),
            ),
        )
        val decisions = autoSelectFastest(sorted) { false }
        assertEquals(1, decisions.size)
        assertEquals(MirrorCategory.ALPINE, decisions[0].first)
        assertEquals("alpine.fast", decisions[0].second.mirror.id)
    }

    @Test
    fun skipsCategoriesWithPriorSelection() {
        val sorted = mapOf(
            MirrorCategory.ALPINE to listOf(result("alpine.fast", MirrorCategory.ALPINE, 120)),
            MirrorCategory.PIP to listOf(result("pip.fast", MirrorCategory.PIP, 90)),
        )
        // PIP already has a user selection — the speed test must not override it.
        val decisions = autoSelectFastest(sorted) { it == MirrorCategory.PIP }
        assertEquals(1, decisions.size)
        assertEquals(MirrorCategory.ALPINE, decisions[0].first)
        assertEquals("alpine.fast", decisions[0].second.mirror.id)
    }

    @Test
    fun skipsCategoriesWithOnlyFailedResults() {
        val sorted = mapOf(
            MirrorCategory.NPM to listOf(
                result("npm.a", MirrorCategory.NPM, null),
                result("npm.b", MirrorCategory.NPM, null),
            ),
        )
        assertTrue(autoSelectFastest(sorted) { false }.isEmpty())
    }

    @Test
    fun skipsMissingCategories() {
        val sorted = mapOf<MirrorCategory, List<MirrorTestResult>>()
        assertTrue(autoSelectFastest(sorted) { false }.isEmpty())
    }

    @Test
    fun coversAllThreeCategoriesInEnumOrder() {
        val sorted = mapOf(
            MirrorCategory.ALPINE to listOf(result("alpine.fast", MirrorCategory.ALPINE, 100)),
            MirrorCategory.PIP to listOf(result("pip.fast", MirrorCategory.PIP, 200)),
            MirrorCategory.NPM to listOf(result("npm.fast", MirrorCategory.NPM, 300)),
        )
        val decisions = autoSelectFastest(sorted) { false }
        assertEquals(3, decisions.size)
        assertEquals("alpine.fast", decisions[0].second.mirror.id)
        assertEquals("pip.fast", decisions[1].second.mirror.id)
        assertEquals("npm.fast", decisions[2].second.mirror.id)
    }

    @Test
    fun singleCategoryTestSelectsTheMeasuredMirror() {
        // [dv15-B2 / B-02] runTest feeds exactly this shape — a one-entry map for
        // the category the user tapped. The decision must pick that mirror, so
        // the single-category speed test ends up in the rootfs config the same
        // way the full test does.
        val sorted = mapOf(
            MirrorCategory.NPM to listOf(
                result("npm.fast", MirrorCategory.NPM, 80),
                result("npm.slow", MirrorCategory.NPM, 900),
            ),
        )
        val decisions = autoSelectFastest(sorted) { false }
        assertEquals(1, decisions.size)
        assertEquals(MirrorCategory.NPM, decisions[0].first)
        assertEquals("npm.fast", decisions[0].second.mirror.id)
    }

    @Test
    fun singleCategoryTestLeavesAStandingSelectionAlone() {
        // The same contract as skipsCategoriesWithPriorSelection, applied to the
        // one-category map the single-category test builds: a mirror the user
        // picked by hand must survive pressing "test".
        val sorted = mapOf(
            MirrorCategory.NPM to listOf(result("npm.fast", MirrorCategory.NPM, 80)),
        )
        assertTrue(autoSelectFastest(sorted) { it == MirrorCategory.NPM }.isEmpty())
    }

    @Test
    fun failureResultsSortAfterSuccesses() {
        // The caller feeds `sorted` (already ordered by the latency comparator);
        // the decision must take the FIRST successful entry, not the overall
        // first entry — a leading failure must not shadow a later success.
        val sorted = mapOf(
            MirrorCategory.PIP to listOf(
                result("pip.dead", MirrorCategory.PIP, null),
                result("pip.alive", MirrorCategory.PIP, 250),
            ),
        )
        val decisions = autoSelectFastest(sorted) { false }
        assertEquals(1, decisions.size)
        assertEquals("pip.alive", decisions[0].second.mirror.id)
    }
}
