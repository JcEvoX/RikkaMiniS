package probe

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Behaviour half of the FIX-A device (deep-audit-1020 ISSUE-01).
 *
 * Drives the **real** `ProviderRepository.kt` call expression (extracted
 * verbatim by `probe/jvm/extract.py`) against a recording fake registry and
 * asserts the 5th argument is the injected sentinel rather than the `null`
 * default.
 *
 * Expectation is a hardcoded literal (identity against `SENTINEL_CONTEXT`),
 * never derived from the code under test — a self-referential expectation
 * would let the mutation masquerade as a PASS.
 */
class ExtractedCallForwardsContextTest {

    @Test
    fun `the extracted call site forwards the injected context to the registry`() {
        val received = runBlocking { driveExtracted() }
        println("PROBE_LAST_CONTEXT=${describe(received)}")
        assertSame(
            "the registry must receive the injected Context as its 5th argument; " +
                "receiving `null` means the 7-day disk cache read, the 401/403 " +
                "invalidate and the save are all inert (deep-audit-1020 ISSUE-01)",
            SENTINEL_CONTEXT,
            received,
        )
    }

    @Test
    fun `the extracted call really reached the registry`() {
        runBlocking { driveExtracted() }
        assertTrue("extracted call must actually invoke the registry", ModelListProviderRegistry.called)
    }

    private fun describe(v: Any?): String = when {
        v === null -> "null"
        v === SENTINEL_CONTEXT -> "SENTINEL"
        else -> "OTHER(${v.javaClass.simpleName})"
    }
}
