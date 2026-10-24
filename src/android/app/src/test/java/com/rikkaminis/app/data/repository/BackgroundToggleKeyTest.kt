package com.rikkaminis.app.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [dv29-A4] Pins the key → cached-toggle mapping behind
 * [BackgroundSettingsRepository.refreshFromWrite].
 *
 * The device symptom was "the write landed but the consumer kept the old
 * value"; the cheapest way for that to be a *key* bug is a renamed / retyped key
 * on the consumer side, which no behavioural test would catch (a listener that
 * never matches its key looks exactly like a listener that never fires). These
 * assertions use literal key strings on purpose — reading the expectation from
 * [BackgroundSettingsRepository.KEY_TASK_NOTIFICATIONS] would let a rename move
 * both sides together and keep the test green.
 */
class BackgroundToggleKeyTest {

    @Test
    fun taskNotificationsKeyMapsToTaskNotifications() {
        assertEquals(
            BackgroundToggle.TaskNotifications,
            backgroundToggleForKey("taskNotificationsEnabled"),
        )
    }

    @Test
    fun overlayKeyMapsToBackgroundOverlay() {
        assertEquals(
            BackgroundToggle.BackgroundOverlay,
            backgroundToggleForKey("backgroundOverlayEnabled"),
        )
    }

    @Test
    fun unrelatedAndNullKeysMapToNothing() {
        assertNull(backgroundToggleForKey("backgroundOverlayX"))
        assertNull(backgroundToggleForKey("taskNotificationsEnabledV2"))
        assertNull(backgroundToggleForKey(""))
        assertNull(backgroundToggleForKey(null))
    }
}
