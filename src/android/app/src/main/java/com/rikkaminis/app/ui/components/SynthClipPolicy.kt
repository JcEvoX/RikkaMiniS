package com.rikkaminis.app.ui.components

/**
 * [dv26/G10] The decisions behind `synthesizeTestClip`'s "nothing may be written
 * after cancellation" latch, kept as pure functions so they can be driven on a
 * plain JVM (the coroutine + system TTS engine around them cannot).
 *
 * The defect this encodes (deep-audit-1046 S-10): the `TextToSpeech(context)`
 * init callback used to run unconditionally. `withTimeoutOrNull(15_000)` (or the
 * sheet being dismissed) cancels the coroutine, `invokeOnCancellation` shuts the
 * engine down and `finally` deletes `quicktest-clip-<ts>.wav` — and then a late
 * init callback still reached `synthesizeToFile(...)`, writing the clip back into
 * `cacheDir` with nobody left to delete it. The clip is orphaned for the rest of
 * the process lifetime.
 *
 * `cancelled` is the caller's latch (`java.util.concurrent.atomic.AtomicBoolean`)
 * — the engine callbacks run on the main looper while cancellation is delivered
 * from the coroutine's own dispatcher, so the read must be the thread-safe one.
 */
internal object SynthClipPolicy {

    /**
     * May the engine init callback body proceed into the part that touches the
     * engine (`language`, progress listener, `synthesizeToFile`)? A callback that
     * lands after cancellation must return before any of it.
     */
    fun mayTouchEngine(cancelled: Boolean): Boolean = !cancelled

    /**
     * May `synthesizeToFile` be dispatched? Checked separately from
     * [mayTouchEngine] because the two are separated by several statements
     * (`language`, `setOnUtteranceProgressListener`) — the request can be
     * cancelled inside that window.
     */
    fun maySynthesize(cancelled: Boolean): Boolean = !cancelled

    /**
     * An engine progress callback (`onDone`/`onError`) that lands after
     * cancellation reports a write we are no longer waiting for: the clip exists
     * on disk and the caller has already walked away, so it must be dropped.
     */
    fun mustDropOnCallback(cancelled: Boolean): Boolean = cancelled
}
