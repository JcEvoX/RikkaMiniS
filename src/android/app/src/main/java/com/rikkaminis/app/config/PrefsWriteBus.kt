package com.rikkaminis.app.config

import android.content.SharedPreferences
import java.util.concurrent.CopyOnWriteArrayList

/**
 * [dv29-A4] Write-through delivery for prefs-backed config fields.
 *
 * Background. `minis-config set background.overlay true` persists the value and
 * `minis-config get background.overlay` reads it back as `true`, yet the live
 * consumer of that same key keeps serving the previous value until the process
 * restarts: on the device (app `1.0.0+2597`, main `b82aa8dc`) the write landed
 * at `13:46:46.155` while `AgentForegroundService.applyOverlayState` went on
 * logging `enabled=false` at `13:46:48.408`, `13:46:50.403`, … — same pid
 * (`23883`) for the offload handler and the service, so this is not a
 * cross-process reading. The only refresh layer between the two was
 * [com.rikkaminis.app.data.repository.BackgroundSettingsRepository]'s
 * `OnSharedPreferenceChangeListener`: a *passive* callback that has to be
 * invoked by the very `SharedPreferences` instance the field wrote through.
 *
 * That is the shape this file removes. A prefs-backed field now hands the value
 * it just wrote to every registered [Sink] — the write path actively delivers
 * instead of assuming a notification will arrive later. Two properties matter:
 *
 *  * The sink receives the **writer's** `SharedPreferences` handle. It must read
 *    the new value from that handle, never from one it cached itself: a
 *    consumer that re-reads its own handle is only correct while both handles
 *    are the same object, which is exactly the assumption that failed on the
 *    device. Reading through the passed handle is correct either way.
 *  * Delivery is sequenced *after* the edit was applied, so a sink can never
 *    observe a value older than the one being reported.
 *
 * The passive listener stays in place as a cross-instance / non-field fallback
 * (UI setters, backup restore writing a raw key, any `prefs.edit()` outside this
 * layer). Nothing here replaces it; this closes the case where it does not fire.
 *
 * Same shape as [SessionConfigWriteThrough] ([dv24-A05]) one layer over: there a
 * `session.*` write landed in the Room row and the resident view model kept the
 * value it read at `loadSession()`; here the write lands in prefs and the cached
 * StateFlow keeps its construction-time value. In both cases the fix is to have
 * the writer deliver to the live surface instead of trusting a passive refresh.
 *
 * Scope: in-process only. `AndroidManifest.xml` declares two side processes
 * (`:modelservice`, `:toolservice`) and neither reads the config prefs files, so
 * a broadcast / multi-process channel would be machinery without a consumer.
 *
 * Failure posture: a sink must never break a config write that already landed,
 * so a throwing sink is contained and reported — the remaining sinks still run
 * and the caller sees a normal success.
 */
object PrefsWriteBus {

    /**
     * A prefs consumer that wants the just-written value pushed to it.
     *
     * [prefs] is the handle the write went through; [key] is the prefs key.
     * Implementations should ignore keys they do not own (a key comparison is
     * enough — the same key name in two files is harmless: re-reading an
     * unchanged value leaves the consumer's state identical).
     */
    fun interface Sink {
        fun onPrefsWrite(prefs: SharedPreferences, key: String)
    }

    private val sinks = CopyOnWriteArrayList<Sink>()

    /**
     * Overridable so JVM tests (which have no Android log/file stack) can
     * observe a contained sink failure. Production keeps the AppLogger default.
     */
    internal var onSinkFailure: ((String, Throwable) -> Unit)? = { key, t ->
        com.rikkaminis.app.logging.AppLogger.warning(
            "PrefsWriteBus",
            "sink failed for key=$key: ${t.javaClass.simpleName}: ${t.message}",
        )
    }

    /** Registration is idempotent per sink identity (same rule as prefs listeners). */
    fun register(sink: Sink) {
        if (!sinks.contains(sink)) sinks.add(sink)
    }

    fun unregister(sink: Sink) {
        sinks.remove(sink)
    }

    /**
     * Deliver a completed write of [key] to every sink.
     *
     * Called by the `Prefs*Field` writers in [com.rikkaminis.app.config.fields]
     * after `apply()`, i.e. after the in-memory map already holds the new value.
     */
    fun publish(prefs: SharedPreferences, key: String) {
        if (sinks.isEmpty()) return
        for (sink in sinks) {
            try {
                sink.onPrefsWrite(prefs, key)
            } catch (t: Throwable) {
                // Contained: the write already landed, and one consumer's bug
                // must not turn a successful config write into a failure (nor
                // starve the sinks after it).
                onSinkFailure?.invoke(key, t)
            }
        }
    }
}
