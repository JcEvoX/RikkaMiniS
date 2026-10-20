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

    // FLAG_LAYOUT_NO_LIMITS = 0x00000100
    private val flagLayoutNoLimits: Int = 0x00000100
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
        // 0x00000100 or 0x80000000 == 0x80000100 — dead literal.
        assertEquals(0x80000100.toInt(), galleryDialogWindowFlags())
    }
}
