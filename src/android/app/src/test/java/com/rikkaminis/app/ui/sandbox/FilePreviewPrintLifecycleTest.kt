package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1049 B-05] The print slot's release rules, pinned on the JVM.
 *
 * `printFile` keeps its WebView in a module-level slot because
 * `createPrintDocumentAdapter` reads that view lazily for as long as the job
 * spools. Three things did not happen around it: the slot was overwritten with
 * no release of the previous instance, a superseded view's `onPageFinished`
 * still pulled a second print dialog, and leaving the preview screen released
 * nothing at all.
 *
 * The Android half (WebView / PrintManager / DisposableEffect) needs a device;
 * what decides *whether* an instance may be torn down is pure, and that is what
 * is asserted here. Every expectation is a literal — never derived from the
 * production constants, so changing those cannot also change the test.
 */
class FilePreviewPrintLifecycleTest {

    @Test
    fun `nothing to release when the slot is empty`() {
        assertFalse(shouldReleasePrintSlot(hasSlot = false, slotSpooling = false))
    }

    @Test
    fun `an idle slot is released before it is re-claimed`() {
        assertTrue(shouldReleasePrintSlot(hasSlot = true, slotSpooling = false))
    }

    @Test
    fun `a spooling view is never torn down`() {
        // createPrintDocumentAdapter reads the WebView lazily; destroying it
        // here prints blank pages, which is why the release is conditional.
        assertFalse(shouldReleasePrintSlot(hasSlot = true, slotSpooling = true))
        assertFalse(shouldReleasePrintSlot(hasSlot = false, slotSpooling = true))
    }

    @Test
    fun `the slot owner is compared by identity`() {
        val owner = mutableListOf("job")
        assertTrue(isCurrentPrintView(owner, owner))
        // EQUAL but not identical: releasing this one instead of the slot owner
        // would tear down a renderer a print job is still reading. (`==` on two
        // empty-or-equal lists is true, so an equality comparison passes the
        // first assertion and fails this one.)
        assertFalse(isCurrentPrintView(owner, mutableListOf("job")))
    }

    @Test
    fun `an empty slot never owns a late callback`() {
        assertFalse(isCurrentPrintView(null, mutableListOf("job")))
        assertFalse(isCurrentPrintView(null, null))
    }

    @Test
    fun `release decisions are a plain function of the two facts`() {
        // Enumerated on purpose: a truth table that also asserts the NUMBER of
        // true cells catches an inverted or half-broken predicate, which a
        // single spot check does not.
        var released = 0
        for (has in listOf(false, true)) {
            for (spooling in listOf(false, true)) {
                if (shouldReleasePrintSlot(has, spooling)) released++
            }
        }
        assertEquals(1, released)
    }
}
