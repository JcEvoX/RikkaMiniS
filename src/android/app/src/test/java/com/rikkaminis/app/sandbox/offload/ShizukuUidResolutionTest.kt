package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [da1034-A2 ISSUE-04] Pins the uid extraction used by `network restrict/allow`
 * and `network stats`.
 *
 * Two defects, one root: `pkgUid` used to return `"0"` whenever the dumpsys
 * output carried no `userId=`, so
 *  - `network restrict <typo>` ran `cmd netpolicy ... 0` and reported success
 *    (it changed ROOT's policy), and
 *  - `network stats <typo>` reported root's traffic as the package's.
 *
 * The contract this pins: unresolvable ⇒ **null**, and null must be treated as
 * a refusal by the callers (both call sites return OPERATION_FAILED before any
 * command runs / before any number is reported). Expectations are hard-coded
 * literals, not derived from the expression under test.
 */
class ShizukuUidResolutionTest {

    @Test
    fun extractsUserIdFromRealisticDump() {
        val dump = """
            Packages:
              Package [com.example.app] (a1b2c3):
                userId=10234
                pkg=Package{99 com.example.app}
                codePath=/data/app/~~x/com.example.app-y
                versionName=1.2.3
        """.trimIndent()
        assertEquals("10234", extractUidFromDumpsys(dump))
    }

    @Test
    fun extractsUidWithSharedUserSuffix() {
        // Shared-uid packages print `userId=1000` with no suffix; the capture
        // must stop at the digits either way.
        val dump = "  userId=1000:com.android.shell\n"
        assertEquals("1000", extractUidFromDumpsys(dump))
    }

    @Test
    fun firstUserIdWins() {
        val dump = """
              userId=10111
              pkg=Package{1 com.first}
              userId=10222
        """.trimIndent()
        assertEquals("10111", extractUidFromDumpsys(dump))
    }

    @Test
    fun unknownPackageYieldsNull() {
        // The old code returned "0" here — the whole defect.
        val dump = "Unable to find package: com.does.not.exist\n"
        assertNull(extractUidFromDumpsys(dump))
    }

    @Test
    fun emptyOutputYieldsNull() {
        assertNull(extractUidFromDumpsys(""))
    }

    @Test
    fun uidShapedTextWithoutTheKeyYieldsNull() {
        // No `userId=` key: a bare number must not be mistaken for a uid.
        assertNull(extractUidFromDumpsys("  uid: 10123\n  appId: 10123\n"))
    }

    @Test
    fun nullIsNotZero() {
        // The regression guard, spelled out: the failure value must NOT be the
        // string "0" (which is root's uid).
        val unresolved: String? = extractUidFromDumpsys("no matches here")
        assertNull(unresolved)
        assertEquals(false, unresolved == "0")
    }
}
