package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25-B1 / deep-audit-1045 B-01] JVM tests for [decideAutoApply] — the pure
 * decision behind first-boot mirror auto-detection.
 *
 * Background: `autoDetectOnceIfNeeded` reuses the normal speed-test flow and
 * then settles on its own. The settlement used to walk every category and write
 * `selectedMirrorId` / `useCustomMirror` / prefs / rootfs config unconditionally,
 * so a mirror the user picked by hand during the first-boot test was silently
 * replaced. It also outlived the run it reused: a user tap cancelled the inner
 * `runAllTests` job, `join()` returned immediately on that cancelled job, and
 * the detector settled anyway with whatever was left in `results`.
 *
 * The decision is now a pure function so the "user selection wins" half is
 * directly testable; the ownership half (the detector job is cancelled by the
 * user entry points) is pinned by the source-shape arms in
 * [MirrorSpeedTestShapeTest]. Expected values are hard-coded literals, never
 * derived from the code under test.
 */
class MirrorAutoDetectDecisionTest {

    private fun entry(id: String, category: MirrorCategory, official: Boolean = false) = MirrorEntry(
        id = id,
        name = id,
        baseURL = "https://example.invalid/$id/",
        testURL = "https://example.invalid/$id/probe",
        category = category,
        region = "Test",
        isOfficial = official,
    )

    private fun reachable(id: String, category: MirrorCategory, latencyMs: Int) =
        MirrorTestResult(entry(id, category), latencyMs, null)

    private fun unreachable(id: String, category: MirrorCategory) =
        MirrorTestResult(entry(id, category), null, "HTTP 500")

    @Test
    fun `skips a category the user already picked by hand`() {
        // The user picked an ALPINE mirror while the first-boot test was still
        // running. Detection must leave that category alone and still apply the
        // categories nobody has an opinion about.
        val results = mapOf(
            MirrorCategory.ALPINE to listOf(reachable("alpine.fast", MirrorCategory.ALPINE, 90)),
            MirrorCategory.PIP to listOf(reachable("pip.fast", MirrorCategory.PIP, 140)),
        )

        val decided = decideAutoApply(results, setOf(MirrorCategory.ALPINE))

        assertEquals(1, decided.size)
        assertEquals(MirrorCategory.PIP, decided[0].first)
        assertEquals("pip.fast", decided[0].second.mirror.id)
    }

    @Test
    fun `applies nothing when every category is already selected`() {
        // Whole-map form of the same contract: three reachable mirrors, three
        // user picks — the detector must not write to any of them.
        val results = mapOf(
            MirrorCategory.ALPINE to listOf(reachable("alpine.fast", MirrorCategory.ALPINE, 90)),
            MirrorCategory.PIP to listOf(reachable("pip.fast", MirrorCategory.PIP, 120)),
            MirrorCategory.NPM to listOf(reachable("npm.fast", MirrorCategory.NPM, 150)),
        )

        assertTrue(
            decideAutoApply(results, MirrorCategory.entries.toSet()).isEmpty(),
        )
    }

    @Test
    fun `applies every unselected category in enum order`() {
        val results = mapOf(
            MirrorCategory.NPM to listOf(reachable("npm.fast", MirrorCategory.NPM, 150)),
            MirrorCategory.ALPINE to listOf(reachable("alpine.fast", MirrorCategory.ALPINE, 90)),
            MirrorCategory.PIP to listOf(reachable("pip.fast", MirrorCategory.PIP, 120)),
        )

        val decided = decideAutoApply(results, emptySet())

        assertEquals(3, decided.size)
        assertEquals(
            listOf(MirrorCategory.ALPINE, MirrorCategory.PIP, MirrorCategory.NPM),
            decided.map { it.first },
        )
        assertEquals(
            listOf("alpine.fast", "pip.fast", "npm.fast"),
            decided.map { it.second.mirror.id },
        )
    }

    @Test
    fun `takes the first successful entry of the caller-ordered list`() {
        // Same input contract as autoSelectFastest: the caller feeds results
        // ordered by the ViewModel's latency comparator (successes ascending,
        // failures last), so the decision takes the first success and never
        // re-sorts. A leading failure must not shadow it.
        val results = mapOf(
            MirrorCategory.PIP to listOf(
                unreachable("pip.dead", MirrorCategory.PIP),
                reachable("pip.fast", MirrorCategory.PIP, 200),
                reachable("pip.slower", MirrorCategory.PIP, 700),
            ),
        )

        val decided = decideAutoApply(results, emptySet())

        assertEquals(1, decided.size)
        assertEquals("pip.fast", decided[0].second.mirror.id)
    }

    @Test
    fun `never auto-selects the official CDN`() {
        // On first boot the official source is what the config already points
        // at — choosing it would be a no-op that merely flips useCustomMirror.
        val results = mapOf(
            MirrorCategory.ALPINE to listOf(
                MirrorTestResult(
                    entry("alpine.official", MirrorCategory.ALPINE, official = true),
                    40,
                    null,
                ),
            ),
        )

        assertTrue(decideAutoApply(results, emptySet()).isEmpty())
    }

    @Test
    fun `skips categories whose probes all failed`() {
        val results = mapOf(
            MirrorCategory.NPM to listOf(
                unreachable("npm.a", MirrorCategory.NPM),
                unreachable("npm.b", MirrorCategory.NPM),
            ),
        )

        assertTrue(decideAutoApply(results, emptySet()).isEmpty())
    }

    @Test
    fun `skips categories missing from the result map`() {
        // The detector runs right after the reused runAllTests; if a category
        // produced no results at all, nothing can be decided for it.
        val results = mapOf(
            MirrorCategory.ALPINE to listOf(reachable("alpine.fast", MirrorCategory.ALPINE, 90)),
        )

        val decided = decideAutoApply(results, emptySet())

        assertEquals(1, decided.size)
        assertEquals(MirrorCategory.ALPINE, decided[0].first)
    }

    @Test
    fun `single category already picked yields nothing`() {
        // The exact window from the report: the user tapped one category's test
        // while first-boot detection was settling. Detection must not undo the
        // mirror that test just selected.
        val results = mapOf(
            MirrorCategory.NPM to listOf(reachable("npm.fast", MirrorCategory.NPM, 80)),
        )

        assertTrue(decideAutoApply(results, setOf(MirrorCategory.NPM)).isEmpty())
    }
}
