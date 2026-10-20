package com.rikkaminis.app.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv14-B1] JVM-pure test for the gallery Dialog window flag contract
 * (`galleryDialogWindowFlags`, ImageGalleryViewer.kt).
 *
 * Bug context (ISSUE-01): the OUTER `DisposableEffect(Unit)` took its window
 * from `findDialogWindow(view)` where `view` is the host composition's view —
 * its parent chain (ContentFrameLayout → FitWindowsLinearLayout → DecorView)
 * never contains a DialogWindowProvider, so `window` was null on every entry
 * and the hide/restore block was a silent no-op. The fix restores
 * `(view.context as? android.app.Activity)?.window` for the host window, and
 * the Dialog's own window now applies
 * FLAG_LAYOUT_NO_LIMITS | FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS via
 * [galleryDialogWindowFlags] — without the latter the dialog leaves a grey
 * band under the status bar even with statusBarColor = TRANSPARENT.
 *
 * The window-source decision itself needs a real View hierarchy (no Android
 * SDK in the JVM here) and is pinned by the
 * webview_holder_url_source_check gate (W6a/W6b) plus a shape-assertion
 * script instead.
 *
 * Expected values are dead literals, NOT derived from the same
 * WindowManager.LayoutParams constants the production function composes —
 * a constant change must not silently drag the expectation with it.
 */
class ImageGalleryWindowContractTest {

    // FLAG_LAYOUT_NO_LIMITS = 0x00000200 — NOT 0x00000100 (that is
    // FLAG_LAYOUT_IN_SCREEN; the first CI run red-flagged exactly this:
    // expected -2147483392 (0x80000100, wrong memory) vs was -2147483136
    // (0x80000200, real android.jar). Dead literals stay, values from CI
    // ground truth.
    private val flagLayoutNoLimits: Int = 0x00000200
    // FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS = 0x80000000 (Int.MIN_VALUE as Int)
    private val flagDrawsSystemBarBackgrounds: Int = 0x80000000.toInt()

    @Test fun dialogWindowFlagsIncludeLayoutNoLimits() {
        val flags = galleryDialogWindowFlags()
        assertTrue(
            "gallery dialog window must extend under the display cutout (FLAG_LAYOUT_NO_LIMITS)",
            (flags and flagLayoutNoLimits) != 0,
        )
    }

    @Test fun dialogWindowFlagsIncludeDrawsSystemBarBackgrounds() {
        val flags = galleryDialogWindowFlags()
        assertTrue(
            "gallery dialog window must paint its own system-bar background (no grey band)",
            (flags and flagDrawsSystemBarBackgrounds) != 0,
        )
    }

    @Test fun dialogWindowFlagsComposeToTheExactExpectedValue() {
        // 0x00000200 or 0x80000000 == 0x80000200 — dead literal, verified
        // against CI ground truth (was:<-2147483136>).
        assertEquals(0x80000200.toInt(), galleryDialogWindowFlags())
    }
}
