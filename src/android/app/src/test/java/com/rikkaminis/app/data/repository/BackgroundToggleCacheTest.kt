package com.rikkaminis.app.data.repository

import com.rikkaminis.app.config.FakeSharedPreferences
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * [dv29-A4] The device-case property, on the production refresh path.
 *
 * App `1.0.0+2597`: `minis-config set background.overlay true` persisted (a
 * follow-up `get` read `true`) while `AgentForegroundService` went on logging
 * `enabled=false` for minutes in the same pid. Whatever the sub-mechanism — a
 * SharedPreferences callback that never fired, or the registry and this
 * repository holding two handles over the same file — the refresh must not
 * depend on the two handles being the same object.
 *
 * With one handle the two explanations are indistinguishable, so these tests
 * state the stronger contract with two handles: the cache refreshes from the
 * handle the write went through, even though its own handle is still stale.
 */
class BackgroundToggleCacheTest {

    @Test
    fun defaultsComeFromPrefsWhenKeysAreAbsent() {
        val cache = BackgroundToggleCache(FakeSharedPreferences())

        // Literals, not the DEFAULT_* constants: reading the expectation from the
        // declaration would let a flipped default move both sides together.
        assertEquals(true, cache.taskNotificationsEnabled.value)
        assertEquals(false, cache.backgroundOverlayEnabled.value)
    }

    @Test
    fun overlayRefreshesFromWritersHandleEvenWhenOwnHandleIsStale() {
        // The consumer's handle carries the pre-write value…
        val consumerHandle = FakeSharedPreferences(mapOf("backgroundOverlayEnabled" to false))
        val cache = BackgroundToggleCache(consumerHandle)
        assertEquals(false, cache.backgroundOverlayEnabled.value)

        // …while the external write lands on another handle over the same file.
        val writerHandle = consumerHandle.cloneHandle()
        writerHandle.edit().putBoolean("backgroundOverlayEnabled", true).apply()

        // The production sink, not a test-local one: this is the wiring that
        // ships, so a `applyWrite(prefs, key)` regression fails here.
        cache.writeSink.onPrefsWrite(writerHandle, "backgroundOverlayEnabled")

        assertEquals(true, cache.backgroundOverlayEnabled.value)
        // Documents exactly what the device showed: the consumer's own handle
        // never saw the write, so re-reading it would have kept the stale value.
        assertFalse(consumerHandle.getBoolean("backgroundOverlayEnabled", true))
    }

    @Test
    fun passiveListenerStillRefreshesSameHandleWrites() {
        val handle = FakeSharedPreferences()
        val cache = BackgroundToggleCache(handle)
        assertEquals(false, cache.backgroundOverlayEnabled.value)

        // Fallback path: a write that bypasses PrefsWriteBus, reported by the
        // SharedPreferences callback (same handle, as AOSP delivers it).
        handle.edit().putBoolean("backgroundOverlayEnabled", true).apply()
        cache.prefsListener.onSharedPreferenceChanged(handle, "backgroundOverlayEnabled")

        assertEquals(true, cache.backgroundOverlayEnabled.value)
    }

    @Test
    fun taskNotificationsRefreshesFromWritersHandle() {
        val consumerHandle = FakeSharedPreferences() // default ON
        val cache = BackgroundToggleCache(consumerHandle)
        assertEquals(true, cache.taskNotificationsEnabled.value)

        val writerHandle = consumerHandle.cloneHandle()
        writerHandle.edit().putBoolean("taskNotificationsEnabled", false).apply()

        cache.applyWrite(writerHandle, "taskNotificationsEnabled")

        assertEquals(false, cache.taskNotificationsEnabled.value)
    }

    @Test
    fun unrelatedAndNullKeysLeaveStateUntouched() {
        val handle = FakeSharedPreferences()
        val cache = BackgroundToggleCache(handle)

        for (key in listOf("backgroundOverlayX", "backgroundOverlayY", "", null)) {
            cache.applyWrite(handle, key)
        }

        assertEquals(true, cache.taskNotificationsEnabled.value)
        assertEquals(false, cache.backgroundOverlayEnabled.value)
    }

    @Test
    fun inAppSetterStillDrivesTheFlow() {
        val cache = BackgroundToggleCache(FakeSharedPreferences())

        cache.setBackgroundOverlayEnabled(true)
        cache.setTaskNotificationsEnabled(false)

        assertEquals(true, cache.backgroundOverlayEnabled.value)
        assertEquals(false, cache.taskNotificationsEnabled.value)
    }
}
