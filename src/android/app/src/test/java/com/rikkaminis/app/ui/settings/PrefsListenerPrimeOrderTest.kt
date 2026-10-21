package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv17-B07] Prime before the cached read, inside the same listener.
 *
 * `compactModelEntryId()` returns the `@Volatile` snapshot (`AgentRuntimeLimitsPrefs.kt:510`);
 * the write that triggered the callback landed in SharedPreferences, but the
 * snapshot only catches up when some listener primes. SharedPreferencesImpl
 * dispatches the registered listeners from a WeakHashMap in unspecified order,
 * so this screen's listener can run BEFORE `AgentRuntimeLimitsPrefs`' own
 * self-sync listener — without its own prime the read publishes the pre-write
 * value, and because the screen's Save re-persists every knob it shows, the
 * external write would be silently rolled back. The prime must therefore sit
 * BEFORE the read in the listener body (the belt `RuntimeLimitsScreen` already
 * wears).
 *
 * The screen is a `@Composable` and `SharedPreferencesImpl` is real-Android
 * (no in-sandbox substitute), so this is a SHAPE test on the real shipped
 * source — the house pattern of `ProviderEntryKeyAndPinTest`. Every
 * expectation is a literal; the listener body is extracted by balanced braces
 * (not `indexOf` with a starting offset — that shape was the 1037-B false red)
 * so a mutation that deletes the prime line, moves it after the read, or
 * inlines the listener turns this red.
 */
class PrefsListenerPrimeOrderTest {

    private val screen =
        "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ModelGroupsScreen.kt"

    @Test
    fun `the compact-pin listener primes before reading the cache (dv17-B07)`() {
        val src = readRepoFile(screen)
        val rest = src.substringAfter(
            "val listener = SharedPreferences.OnSharedPreferenceChangeListener",
        )
        val open = rest.indexOf('{')
        require(open >= 0) { "the listener lambda lost its body" }
        var depth = 0
        var close = -1
        for (j in open until rest.length) {
            if (rest[j] == '{') depth++
            if (rest[j] == '}') {
                depth--
                if (depth == 0) {
                    close = j
                    break
                }
            }
        }
        require(close >= 0) { "the listener lambda body is unbalanced" }
        val body = rest.substring(open, close + 1)
        val read = body.indexOf("AgentRuntimeLimitsPrefs.compactModelEntryId()")
        val prime = body.indexOf("AgentRuntimeLimitsPrefs.prime(")
        assertTrue("the listener must still re-read the cached pin", read >= 0)
        assertTrue(
            "the listener must prime the cache before the cached read (dv17-B07)",
            prime in 0 until read,
        )
    }

    /**
     * Walk up from the test working directory to the repo root containing
     * [relative]. Gradle sets user.dir to the module dir; a sandbox JVM run
     * can start anywhere under the repo — no fixed depth works.
     */
    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }
}
