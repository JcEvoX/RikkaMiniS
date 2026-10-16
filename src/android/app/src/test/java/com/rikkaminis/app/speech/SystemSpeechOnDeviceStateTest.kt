package com.rikkaminis.app.speech

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1017 G4] `usingOnDevice` must describe the recognizer that was
 * created, not the one that was requested.
 *
 * `preferOffline` is a request. When `createOnDeviceSpeechRecognizer` throws, the
 * session silently runs on the default recognizer — and `onError`'s "the
 * on-device recognizer has no pack for this locale" retry is gated on this flag.
 * A flag that says on-device for a session running on the default recognizer
 * sends that retry down a path that cannot help (one extra dead session and a
 * mic blip); one that says default when the session really is on-device skips a
 * retry that would have worked. Both directions are pinned, with literal
 * expectations.
 */
class SystemSpeechOnDeviceStateTest {

    @Test
    fun `the on-device recognizer counts as on-device when it was created`() {
        assertTrue(SpeechOnDevicePlan.effective(requestedOnDevice = true, onDeviceCreationFailed = false))
    }

    @Test
    fun `a failed on-device creation is reported as not on-device`() {
        assertFalse(SpeechOnDevicePlan.effective(requestedOnDevice = true, onDeviceCreationFailed = true))
    }

    @Test
    fun `not requesting on-device is never on-device`() {
        assertFalse(SpeechOnDevicePlan.effective(requestedOnDevice = false, onDeviceCreationFailed = false))
        assertFalse(SpeechOnDevicePlan.effective(requestedOnDevice = false, onDeviceCreationFailed = true))
    }
}
