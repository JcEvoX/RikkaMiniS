package com.rikkaminis.app.webapp

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.os.Build
import android.widget.Toast
import androidx.core.content.pm.ShortcutInfoCompat
import androidx.core.content.pm.ShortcutManagerCompat
import androidx.core.graphics.drawable.IconCompat
import com.rikkaminis.app.R
import com.rikkaminis.app.data.db.WebAppShortcutEntity

/**
 * T-pwa-1 (renamed Pwa → WebApp): pin a WebApp shortcut to the user's
 * launcher. Wraps [ShortcutManagerCompat.requestPinShortcut] with the
 * fallbacks needed for pre-Oreo devices and unsupported launchers.
 */
object ShortcutPinner {

    fun pin(
        context: Context,
        shortcut: WebAppShortcutEntity,
        iconBitmap: Bitmap?,
    ): Boolean {
        if (!ShortcutManagerCompat.isRequestPinShortcutSupported(context)) {
            Toast.makeText(context, R.string.webapp_pin_unsupported, Toast.LENGTH_LONG).show()
            return false
        }

        val icon: IconCompat = when {
            iconBitmap == null -> IconCompat.createWithResource(
                context, R.mipmap.ic_launcher,
            )
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O ->
                IconCompat.createWithAdaptiveBitmap(iconBitmap)
            else -> IconCompat.createWithBitmap(iconBitmap)
        }

        val launchIntent = Intent(context, WebAppActivity::class.java).apply {
            action = WebAppActivity.ACTION_OPEN_WEBAPP
            putExtra(WebAppActivity.EXTRA_SHORTCUT_ID, shortcut.id)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }

        val info = ShortcutInfoCompat.Builder(context, "webapp_${shortcut.id}")
            .setShortLabel(shortcut.title)
            .setLongLabel(shortcut.title)
            .setIcon(icon)
            .setIntent(launchIntent)
            .build()

        // [fix/dv21-b4-B08] The failure toast used to live in `getOrElse`, i.e.
        // it only ran when the call THREW. A launcher that advertises support
        // and then answers `false` — the usual answer on MIUI / HyperOS /
        // ColorOS when the "create shortcut" permission was never granted —
        // fell straight through: `AddToHomeSheet` rolled its DB row back and
        // closed the sheet, and the user got a vanishing panel with no icon and
        // no reason. `false` and a throw are the same answer to the user.
        val outcome = pinOutcome(
            runCatching { ShortcutManagerCompat.requestPinShortcut(context, info, null) },
        )
        if (outcome != ShortcutPinOutcome.REQUESTED) {
            Toast.makeText(context, R.string.webapp_pin_failed, Toast.LENGTH_LONG).show()
        }
        return outcome == ShortcutPinOutcome.REQUESTED
    }
}

/**
 * [fix/dv21-b4-B08] The verdict of one `requestPinShortcut` attempt, shared by
 * the two carriers that pin a shortcut — this file and
 * [com.rikkaminis.app.ui.preview.WebPreviewShortcut].
 *
 * Why one shared verdict instead of two hand-written `try/catch` blocks: the
 * carriers drifted. This one toasted only in its `getOrElse` (thrown
 * exceptions), the preview one had neither a failure toast nor a `runCatching`
 * at all — so a throwing OEM launcher took the process down, and a declining
 * one produced no feedback anywhere. Both are the same question ("did the pin
 * actually get requested?"), so they get one answer.
 *
 * `false` is NOT success: the launcher has already told us it will not pin.
 * Only [ShortcutPinOutcome.REQUESTED] may report success to the user.
 *
 * `isRequestPinShortcutSupported` is deliberately NOT folded in: the two call
 * sites carry different "this launcher has no such feature" strings, and each
 * already toasts on that path.
 */
internal enum class ShortcutPinOutcome {
    /** The launcher accepted the request (it may still ask the user to confirm). */
    REQUESTED,

    /** `requestPinShortcut` returned `false` — advertised support, then declined. */
    REJECTED,

    /** `requestPinShortcut` threw (SecurityException / IllegalStateException on
     *  launchers that gate shortcut creation behind an OEM permission). */
    THREW,
}

internal fun pinOutcome(request: Result<Boolean>): ShortcutPinOutcome = when {
    request.isFailure -> ShortcutPinOutcome.THREW
    request.getOrNull() == true -> ShortcutPinOutcome.REQUESTED
    else -> ShortcutPinOutcome.REJECTED
}
