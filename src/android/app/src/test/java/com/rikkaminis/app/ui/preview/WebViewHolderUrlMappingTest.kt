package com.rikkaminis.app.ui.preview

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [dv14-B1] JVM-pure test for the mobile viewport policy single source
 * (`mobileViewportSettings`, WebViewHolder.kt) plus one regression anchor on
 * the asset-loader mapping pair.
 *
 * Bug context (ISSUE-02): `toggleDesktopMode()`'s mobile `else` branch used to
 * set `useWideViewPort = false` and `loadWithOverviewMode = false` — the exact
 * opposite of the init defaults (:127-128) — collapsing `100vh` pages to a
 * zero-height box and locking the layout to a narrow viewport after one
 * Desktop → Mobile round-trip. The fix routes the restore through the
 * single-sourced [mobileViewportSettings] policy so the toggle path cannot
 * drift from the init defaults.
 *
 * The deep two-way URL mapping coverage lives in AssetLoaderUrlMappingTest /
 * FileUriDecodeRoundTripTest; only a last-segment anchor is repeated here.
 *
 * Expected values are dead literals, not derived from the policy defaults —
 * a policy change must not silently drag the expectation with it.
 */
class WebViewHolderUrlMappingTest {

    // ---- dv14-B1: mobile viewport policy (single-sourced contract) --------

    @Test fun mobileViewportPolicyKeepsWideViewportOn() {
        // Must match the init defaults (:127-128): honor the page's
        // <meta name="viewport">.
        assertEquals(true, mobileViewportSettings().useWideViewPort)
    }

    @Test fun mobileViewportPolicyKeepsOverviewModeOn() {
        // Shrink-to-fit stays on in mobile mode; flipping it off is the
        // defect this contract pins.
        assertEquals(true, mobileViewportSettings().loadWithOverviewMode)
    }

    @Test fun mobileViewportPolicyResetsTheDesktopScale() {
        assertEquals(0, mobileViewportSettings().initialScale)
    }

    // ---- regression anchor: forward mapping takes the LAST segment ---------

    @Test fun nestedPathForwardMappingTakesOnlyTheLastSegment() {
        // Scope: the loader's root is the initial file's PARENT directory, so
        // only the last path segment is meaningful on the forward mapping.
        assertEquals(
            "https://appassets.androidplatform.net/report.html",
            toAssetLoaderUrl("file:///data/x/deep/nested/report.html"),
        )
    }
}
