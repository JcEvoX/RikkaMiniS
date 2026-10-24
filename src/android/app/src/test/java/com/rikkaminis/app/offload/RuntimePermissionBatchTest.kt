package com.rikkaminis.app.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [auto-approve] The batch runtime-permission list per API level. Sizes are
 * hard-coded literals so an accidental addition/removal flips the test.
 */
class RuntimePermissionBatchTest {

    @Test
    fun api33PlusUsesMediaPermissionsAndNotifications() {
        val p33 = RuntimePermissionBatch.permissionsFor(33)
        val p34 = RuntimePermissionBatch.permissionsFor(34)
        // 33 and 34 get the same list (no VISUAL_USER_SELECTED on purpose).
        assertEquals(p33, p34)
        assertEquals(11, p33.size)
        assertTrue(p33.contains(RuntimePermissionBatch.READ_MEDIA_IMAGES))
        assertTrue(p33.contains(RuntimePermissionBatch.READ_MEDIA_VIDEO))
        assertTrue(p33.contains(RuntimePermissionBatch.POST_NOTIFICATIONS))
        assertFalse(p33.contains(RuntimePermissionBatch.READ_EXTERNAL_STORAGE))
        // 34 must not add the partial-photo picker permission.
        assertFalse(p33.any { it.contains("VISUAL_USER_SELECTED") })
    }

    @Test
    fun preApi33UsesLegacyStoragePermission() {
        val p32 = RuntimePermissionBatch.permissionsFor(32)
        val p26 = RuntimePermissionBatch.permissionsFor(26)
        assertEquals(p32, p26)
        assertEquals(9, p32.size)
        assertTrue(p32.contains(RuntimePermissionBatch.READ_EXTERNAL_STORAGE))
        assertFalse(p32.contains(RuntimePermissionBatch.READ_MEDIA_IMAGES))
        assertFalse(p32.contains(RuntimePermissionBatch.POST_NOTIFICATIONS))
    }

    @Test
    fun evergreenCoreIsAlwaysPresent() {
        for (sdk in intArrayOf(26, 30, 32, 33, 34, 35)) {
            val p = RuntimePermissionBatch.permissionsFor(sdk)
            for (constant in arrayOf(
                RuntimePermissionBatch.READ_CALENDAR,
                RuntimePermissionBatch.WRITE_CALENDAR,
                RuntimePermissionBatch.READ_CONTACTS,
                RuntimePermissionBatch.WRITE_CONTACTS,
                RuntimePermissionBatch.ACCESS_FINE_LOCATION,
                RuntimePermissionBatch.ACCESS_COARSE_LOCATION,
                RuntimePermissionBatch.RECORD_AUDIO,
                RuntimePermissionBatch.CAMERA,
            )) {
                assertTrue("sdk=$sdk missing $constant", p.contains(constant))
            }
        }
    }

    @Test
    fun allConstantsAreFullyQualified() {
        for (constant in arrayOf(
            RuntimePermissionBatch.READ_CALENDAR,
            RuntimePermissionBatch.WRITE_CALENDAR,
            RuntimePermissionBatch.READ_CONTACTS,
            RuntimePermissionBatch.WRITE_CONTACTS,
            RuntimePermissionBatch.ACCESS_FINE_LOCATION,
            RuntimePermissionBatch.ACCESS_COARSE_LOCATION,
            RuntimePermissionBatch.RECORD_AUDIO,
            RuntimePermissionBatch.CAMERA,
            RuntimePermissionBatch.READ_MEDIA_IMAGES,
            RuntimePermissionBatch.READ_MEDIA_VIDEO,
            RuntimePermissionBatch.READ_EXTERNAL_STORAGE,
            RuntimePermissionBatch.POST_NOTIFICATIONS,
        )) {
            assertTrue(constant.startsWith("android.permission."))
        }
    }
}
