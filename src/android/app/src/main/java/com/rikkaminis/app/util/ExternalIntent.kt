package com.rikkaminis.app.util

import android.content.Context
import android.content.Intent
import com.rikkaminis.app.logging.AppLogger

/**
 * [deep-audit-1049 B-03] The single launch point for "hand this to another app".
 *
 * Every external hand-off in this app needs the same two things, and before
 * this helper each of the six share/open entry points spelled them by hand —
 * getting a different half right each time:
 *
 * **`FLAG_ACTIVITY_NEW_TASK` on the intent that is actually started.** An
 * `Intent.createChooser(...)` does NOT inherit the inner intent's flags, so
 * three of the six sites put the flag on the inner intent and started a bare
 * chooser, one put it on neither, and the Activity-context sites worked by
 * accident (an Activity's own context is exempt). The exemption is invisible at
 * the call site: the same `openExternally(context, …)` runs from an Application
 * context in another caller and throws `AndroidRuntimeException`, which is an
 * `Error`-class crash rather than a caught exception.
 *
 * **A failure the user can see.** "The share key did nothing" is the report
 * this shape produced; a `catch (_: Throwable) {}` cannot distinguish a device
 * with no target from a working share. The launch is logged unconditionally
 * (diagnosable from logcat even when the caller passes no callback) and
 * [onFailure] lets the caller surface it.
 *
 * Callers keep ownership of the *content*: this helper only decides how the
 * intent is launched, never which flags grant URI access (that stays with the
 * intent the caller built).
 */
internal object ExternalIntent {

    /**
     * Start [target] as-is (no chooser). The flag is added to the caller's own
     * instance, which is safe because every call site builds it in place.
     */
    fun start(
        context: Context,
        target: Intent,
        onFailure: ((Throwable) -> Unit)? = null,
    ): Boolean {
        target.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return launch(context, target, onFailure)
    }

    /**
     * Start [target] through a system share/open chooser labelled [title]
     * (`null` = the platform default label, e.g. "Share with").
     */
    fun share(
        context: Context,
        target: Intent,
        title: String?,
        onFailure: ((Throwable) -> Unit)? = null,
    ): Boolean {
        val chooser = Intent.createChooser(target, title)
        chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return launch(context, chooser, onFailure)
    }

    /**
     * One `startActivity`, one catch. Deliberately catches `Throwable`, not
     * `ActivityNotFoundException`: `SecurityException` (a provider the receiver
     * may not read) and `RuntimeException` (no NEW_TASK) both escape a narrower
     * catch and take the process down.
     */
    private fun launch(
        context: Context,
        top: Intent,
        onFailure: ((Throwable) -> Unit)?,
    ): Boolean {
        return try {
            context.startActivity(top)
            true
        } catch (t: Throwable) {
            AppLogger.warning(
                "ExternalIntent",
                "startActivity failed (${top.action ?: "?"}): ${t.javaClass.simpleName}: ${t.message}",
            )
            onFailure?.invoke(t)
            false
        }
    }
}
