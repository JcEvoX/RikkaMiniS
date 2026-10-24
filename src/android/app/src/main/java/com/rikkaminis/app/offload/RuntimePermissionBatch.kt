package com.rikkaminis.app.offload

/**
 * The runtime-permission set the "grant everything once" button in
 * Settings → Permissions asks for in a single `RequestMultiplePermissions`
 * launch.
 *
 * Why a fixed list and not "whatever is declared in the manifest": Android
 * refuses to show a dialog for permissions that aren't declared, and asking for
 * permissions the app never uses trains users to tap through prompts. This list
 * is exactly the set the `android-*` offload handlers check at call time —
 * see `CalendarOffloadHandler` / `ContactsOffloadHandler` /
 * `LocationOffloadHandler` / `NotificationOffloadHandler` /
 * `PhotosOffloadHandler` / `SpeechOffloadHandler`, plus the camera path in
 * `ChatScreen`.
 *
 * Strings are hard-coded instead of `android.Manifest.permission.*` on purpose:
 * this object is exercised by plain JVM unit tests, where the stub `android.jar`
 * returns null for those fields.
 *
 * Not included, and why:
 *  - `READ_MEDIA_VISUAL_USER_SELECTED` (34+): granting it opens the partial
 *    photo picker, which is a different consent model, not "grant everything".
 *  - `ACCESS_MEDIA_LOCATION`: only reads GPS EXIF; the photo tool doesn't need it.
 *  - `MANAGE_EXTERNAL_STORAGE` / notification access / accessibility /
 *    `SYSTEM_ALERT_WINDOW` / exact alarms / battery exemption: all
 *    settings-page grants with no dialog API, so they cannot be batched here.
 */
internal object RuntimePermissionBatch {

    const val READ_CALENDAR = "android.permission.READ_CALENDAR"
    const val WRITE_CALENDAR = "android.permission.WRITE_CALENDAR"
    const val READ_CONTACTS = "android.permission.READ_CONTACTS"
    const val WRITE_CONTACTS = "android.permission.WRITE_CONTACTS"
    const val ACCESS_FINE_LOCATION = "android.permission.ACCESS_FINE_LOCATION"
    const val ACCESS_COARSE_LOCATION = "android.permission.ACCESS_COARSE_LOCATION"
    const val RECORD_AUDIO = "android.permission.RECORD_AUDIO"
    const val CAMERA = "android.permission.CAMERA"
    const val READ_MEDIA_IMAGES = "android.permission.READ_MEDIA_IMAGES"
    const val READ_MEDIA_VIDEO = "android.permission.READ_MEDIA_VIDEO"
    const val READ_EXTERNAL_STORAGE = "android.permission.READ_EXTERNAL_STORAGE"
    const val POST_NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS"

    /**
     * Ordered list for the given API level. Order is stable and grouped by
     * capability so the system's stacked dialogs appear in a predictable
     * sequence.
     */
    fun permissionsFor(sdkInt: Int): List<String> {
        val out = ArrayList<String>(12)
        out.add(READ_CALENDAR)
        out.add(WRITE_CALENDAR)
        out.add(READ_CONTACTS)
        out.add(WRITE_CONTACTS)
        out.add(ACCESS_FINE_LOCATION)
        out.add(ACCESS_COARSE_LOCATION)
        out.add(RECORD_AUDIO)
        out.add(CAMERA)
        if (sdkInt >= 33) {
            // API 33 replaced READ_EXTERNAL_STORAGE for media with the
            // per-type READ_MEDIA_* permissions.
            out.add(READ_MEDIA_IMAGES)
            out.add(READ_MEDIA_VIDEO)
            out.add(POST_NOTIFICATIONS)
        } else {
            out.add(READ_EXTERNAL_STORAGE)
        }
        return out
    }
}
