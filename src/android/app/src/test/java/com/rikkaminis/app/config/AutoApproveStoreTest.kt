package com.rikkaminis.app.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [auto-approve] Store round-trip: a toggle lands in both the StateFlow and
 * the backing SharedPreferences, and [AutoApproveStore.disableAll] clears the
 * master flag together with its two sub-flags (leaving sub-flags armed behind
 * an OFF master would make the next enable broader than the user last saw).
 *
 * The store is a process-lifetime singleton with an idempotent [install], so
 * the class installs the fake ONCE (companion init) and every test resets
 * state through the public setters.
 */
class AutoApproveStoreTest {

    companion object {
        private val prefs = FakeSharedPreferences()

        init {
            // install() is idempotent and the store is a process singleton, so
            // the fake is wired exactly once for the whole class.
            AutoApproveStore.install(prefs)
        }
    }

    @Before
    fun resetState() {
        AutoApproveStore.disableAll()
    }

    @Test
    fun defaultsAreAllOff() {
        assertFalse(AutoApproveStore.isEnabled)
        assertFalse(AutoApproveStore.isSensitiveEnabled)
        assertFalse(AutoApproveStore.isPrivilegedToolsEnabled)
    }

    @Test
    fun setEnabledPersistsAndPublishes() {
        AutoApproveStore.setEnabled(true)
        assertTrue(AutoApproveStore.isEnabled)
        assertEquals(true, prefs.getBoolean(AutoApproveStore.KEY_ENABLED, false))
    }

    @Test
    fun subFlagsPersistIndependently() {
        AutoApproveStore.setEnabled(true)
        AutoApproveStore.setSensitiveEnabled(true)
        AutoApproveStore.setPrivilegedToolsEnabled(true)
        assertTrue(prefs.getBoolean(AutoApproveStore.KEY_SENSITIVE, false))
        assertTrue(prefs.getBoolean(AutoApproveStore.KEY_PRIVILEGED, false))

        // Toggling one sub-flag must not disturb the others.
        AutoApproveStore.setSensitiveEnabled(false)
        assertTrue(AutoApproveStore.isPrivilegedToolsEnabled)
        assertTrue(AutoApproveStore.isEnabled)
    }

    @Test
    fun disableAllClearsMasterAndSubFlags() {
        AutoApproveStore.setEnabled(true)
        AutoApproveStore.setSensitiveEnabled(true)
        AutoApproveStore.setPrivilegedToolsEnabled(true)

        AutoApproveStore.disableAll()

        assertFalse(AutoApproveStore.isEnabled)
        assertFalse(AutoApproveStore.isSensitiveEnabled)
        assertFalse(AutoApproveStore.isPrivilegedToolsEnabled)
        assertEquals(false, prefs.getBoolean(AutoApproveStore.KEY_ENABLED, true))
        assertEquals(false, prefs.getBoolean(AutoApproveStore.KEY_SENSITIVE, true))
        assertEquals(false, prefs.getBoolean(AutoApproveStore.KEY_PRIVILEGED, true))
    }
}
