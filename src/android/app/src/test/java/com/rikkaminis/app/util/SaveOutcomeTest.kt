package com.rikkaminis.app.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 B-01] The one decision behind "we wrote a bitmap and told
 * the user it worked".
 *
 * BOTH halves can fail without throwing, which is the whole reason this is not
 * two `?: return false` lines at the call site:
 *
 *  * `ContentResolver.openOutputStream(uri)` returns NULL — it does not throw —
 *    so `stream?.use { … }` skips the write silently;
 *  * `Bitmap.compress(...)` returns FALSE, and in statement position
 *    (`file.outputStream().use { bmp.compress(…) }`) that verdict is dropped by
 *    `use`'s own return value.
 *
 * The expectations are written as LITERALS (never derived from the production
 * constant under test): a test that recomputes the expectation would stay green
 * while the decision rots.
 */
class SaveOutcomeTest {

    @Test
    fun aWriteThatOpenedAndEncodedIsSuccess() {
        assertTrue(saveOutcome(streamOpened = true, compressed = true))
    }

    @Test
    fun aNullOutputStreamIsReportedAsFailure() {
        assertFalse("a null stream is the shape `?.use` swallowed — it must be a failure",
            saveOutcome(streamOpened = false, compressed = true))
    }

    @Test
    fun anEncodeThatCompressedNothingIsReportedAsFailure() {
        assertFalse("compress()==false leaves a 0-byte file — it must be a failure",
            saveOutcome(streamOpened = true, compressed = false))
    }

    @Test
    fun bothHalvesFailingIsStillOneFailure() {
        assertFalse(saveOutcome(streamOpened = false, compressed = false))
    }

    @Test
    fun successIsTheConjunctionNotEitherHalf() {
        // The defect's exact shape: each half was checked by nobody, so either
        // one alone "looked" like success. Assert the table, not one row.
        assertEquals(
            listOf(true, false, false, false),
            listOf(
                saveOutcome(true, true),
                saveOutcome(true, false),
                saveOutcome(false, true),
                saveOutcome(false, false),
            ),
        )
    }
}
