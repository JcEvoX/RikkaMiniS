package com.rikkaminis.app.config

import android.content.SharedPreferences
import com.rikkaminis.app.config.fields.PrefsBoolField
import com.rikkaminis.app.config.fields.PrefsIntField
import com.rikkaminis.app.config.fields.PrefsStringField
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv29-A4] Contract tests for [PrefsWriteBus] — the write-through delivery path
 * that replaced "assume the SharedPreferences change callback reaches the
 * consumer" for prefs-backed config fields.
 *
 * Why a fake SharedPreferences instead of Android test infra: the property under
 * test is *handle identity*, which a device A/B cannot isolate cheaply but a JVM
 * test can state exactly — two handles over the same keys, one stale, and the
 * consumer must end up with the value the writer applied.
 */
class PrefsWriteBusTest {

    private val cleanups = ArrayList<() -> Unit>()

    @After
    fun tearDown() {
        for (c in cleanups) c()
        cleanups.clear()
    }

    /** Sink that records every delivery; auto-unregistered after the test. */
    private fun recordingSink(): MutableList<Pair<SharedPreferences, String>> {
        val seen = ArrayList<Pair<SharedPreferences, String>>()
        val sink = PrefsWriteBus.Sink { prefs, key -> seen.add(prefs to key) }
        PrefsWriteBus.register(sink)
        cleanups.add { PrefsWriteBus.unregister(sink) }
        return seen
    }

    private fun boolField(prefs: SharedPreferences, key: String) = PrefsBoolField(
        path = "background.overlay",
        displayName = "Background task overlay",
        description = "",
        prefs = prefs,
        key = key,
        defaultValue = false,
    )

    // ── The write path delivers ──────────────────────────────────────────────

    @Test
    fun boolFieldWritePublishesHandedOverPrefsAndKey() {
        val seen = recordingSink()
        val prefs = FakeSharedPreferences()

        boolField(prefs, "backgroundOverlayEnabled").write(ConfigValue.Bool(true))

        assertEquals(1, seen.size)
        assertEquals("backgroundOverlayEnabled", seen[0].second)
        // The handle is the writer's own instance, not a copy…
        assertTrue(seen[0].first === prefs)
        // …and it already holds the value: delivery happens after apply().
        assertEquals(true, seen[0].first.getBoolean("backgroundOverlayEnabled", false))
    }

    @Test
    fun intAndStringWritersPublishToo() {
        val seen = recordingSink()
        val prefs = FakeSharedPreferences()

        PrefsIntField(
            path = "runtime.maxTurns",
            displayName = "",
            description = "",
            prefs = prefs,
            key = "maxTurns",
            defaultValue = 256,
        ).write(ConfigValue.Int(30))
        PrefsStringField(
            path = "soul.name",
            displayName = "",
            description = "",
            prefs = prefs,
            key = "soul.name",
            defaultValue = "",
        ).write(ConfigValue.Str("Rikka"))

        assertEquals(listOf("maxTurns", "soul.name"), seen.map { it.second })
        // Literal expectations on purpose: deriving them from the field constants
        // would let a constant change mutate the expectation too.
        assertEquals(30, seen[0].first.getInt("maxTurns", -1))
        assertEquals("Rikka", seen[1].first.getString("soul.name", null))
    }

    // ── Regression for the on-device symptom ────────────────────────────────

    /**
     * The device case in miniature: the consumer holds its own handle (B) while
     * the write goes through another handle (A) over the same keys. B still
     * carries the pre-write value — exactly what
     * `AgentForegroundService.applyOverlayState … enabled=false` kept reporting
     * after `minis-config get` already read `true`.
     *
     * The fix must be correct *without* assuming A and B are the same object, so
     * the sink reads from the handle it is handed.
     */
    @Test
    fun consumerRecoversEvenWhenItsOwnHandleIsStale() {
        val consumerPrefs = FakeSharedPreferences(mapOf("backgroundOverlayEnabled" to false))
        val writerPrefs = consumerPrefs.cloneHandle()
        var consumerState = consumerPrefs.getBoolean("backgroundOverlayEnabled", false)
        val sink = PrefsWriteBus.Sink { written, key ->
            if (key == "backgroundOverlayEnabled") {
                consumerState = written.getBoolean(key, false)
            }
        }
        PrefsWriteBus.register(sink)
        cleanups.add { PrefsWriteBus.unregister(sink) }

        boolField(writerPrefs, "backgroundOverlayEnabled").write(ConfigValue.Bool(true))

        assertEquals(true, consumerState)
        // Documents the failure mode the fix removes: the consumer's own handle
        // never saw the write.
        assertFalse(consumerPrefs.getBoolean("backgroundOverlayEnabled", true))
    }

    // ── Containment and idempotence ─────────────────────────────────────────

    @Test
    fun throwingSinkDoesNotBreakWriteOrStarveLaterSinks() {
        val failures = ArrayList<String>()
        val original = PrefsWriteBus.onSinkFailure
        PrefsWriteBus.onSinkFailure = { key, t -> failures.add("$key:${t.javaClass.simpleName}") }
        cleanups.add { PrefsWriteBus.onSinkFailure = original }

        val bad = PrefsWriteBus.Sink { _, _ -> throw IllegalStateException("consumer bug") }
        val delivered = ArrayList<String>()
        val good = PrefsWriteBus.Sink { _, key -> delivered.add(key) }
        PrefsWriteBus.register(bad)
        PrefsWriteBus.register(good)
        cleanups.add { PrefsWriteBus.unregister(bad) }
        cleanups.add { PrefsWriteBus.unregister(good) }

        val prefs = FakeSharedPreferences()
        boolField(prefs, "backgroundOverlayEnabled").write(ConfigValue.Bool(true))

        // The write landed, the later sink ran, and the failure was reported.
        assertEquals(true, prefs.getBoolean("backgroundOverlayEnabled", false))
        assertEquals(listOf("backgroundOverlayEnabled"), delivered)
        assertEquals(listOf("backgroundOverlayEnabled:IllegalStateException"), failures)
    }

    @Test
    fun doubleRegistrationDeliversOnceAndUnregisterStops() {
        val prefs = FakeSharedPreferences()
        var calls = 0
        val sink = PrefsWriteBus.Sink { _, _ -> calls++ }
        PrefsWriteBus.register(sink)
        PrefsWriteBus.register(sink)
        cleanups.add { PrefsWriteBus.unregister(sink) }

        PrefsWriteBus.publish(prefs, "backgroundOverlayEnabled")
        assertEquals(1, calls)

        PrefsWriteBus.unregister(sink)
        PrefsWriteBus.publish(prefs, "backgroundOverlayEnabled")
        assertEquals(1, calls)
    }

    @Test
    fun publishWithNoSinksIsANoOp() {
        // Nothing registered at all: the writer must not blow up on an empty bus.
        PrefsWriteBus.publish(FakeSharedPreferences(), "backgroundOverlayEnabled")
    }
}
