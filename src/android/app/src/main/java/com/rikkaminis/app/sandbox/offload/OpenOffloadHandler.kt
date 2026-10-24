package com.rikkaminis.app.sandbox.offload

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import com.rikkaminis.app.sandbox.NativeOffloadHandler
import com.rikkaminis.app.sandbox.NativeOffloadRequest
import com.rikkaminis.app.sandbox.NativeOffloadResult
import com.rikkaminis.app.util.IntentSanitizer
import com.rikkaminis.app.util.lowercaseIntentScheme
import org.json.JSONObject

/**
 * Host-side implementation of `android-open <url>` — launches any URL or
 * URI the Android system knows how to resolve (https, tel, mailto, geo,
 * market, intent, etc.).
 *
 * Includes a few OEM-aware fallbacks:
 *   • `market:` URIs on Huawei / no-GMS devices → redirect to AppGallery.
 *   • Any `ActivityNotFoundException` → structured error naming the
 *     missing handler instead of crashing.
 *
 * [deep-audit-1016c FIX-B B] `intent:` / `android-app:` URIs keep their full
 * URI semantics (alarm / map / store intents with inline extras still work)
 * but are hardened before startActivity: grant flags, clipData and a
 * same-package `component` target are stripped, so a crafted URI can neither
 * hand a launched activity a URI grant nor start an internal (non-exported)
 * activity of this app. The plain ACTION_VIEW path (http/tel/mailto/geo/
 * market) is untouched.
 *
 * Usage:
 *   android-open <url>
 *   android-open --help
 */
class OpenOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        if (args.hasFlag("h", "help")) return NativeOffloadResult(0, HELP)

        val url = args.positional.firstOrNull()
        if (url.isNullOrBlank()) {
            return NativeOffloadResult(2, "android-open: missing <url>\n$HELP")
        }

        return tryOpen(url, args)
    }

    private fun tryOpen(url: String, args: OffloadArgs, hop: Int = 0): NativeOffloadResult {
        return try {
            // T259: intent: / android-app: URIs encode action + categories +
            // extras + flags inline (e.g. ACTION_SET_ALARM with HOUR/MINUTES
            // extras). Wrapping them in ACTION_VIEW threw all of that away,
            // so resolveActivity returned null and the call fell through to
            // the no_handler error branch. Use Intent.parseUri to honor the
            // full URI semantics; ACTION_VIEW path stays for plain http/tel
            // /mailto/geo/market URLs.
            // [deep-audit-1041 FIX-A6] RFC 3986 makes URI schemes
            // case-insensitive, but `Intent.parseUri` matches `intent:` /
            // `android-app:` case-SENSITIVELY internally and silently degrades
            // `INTENT://…` to a plain ACTION_VIEW over the raw string (which
            // then fails as `no_handler`). Match case-insensitively here AND
            // hand the parser a scheme-normalised copy, so a mixed-case scheme
            // keeps the full URI_INTENT_SCHEME semantics.
            val isIntentUri = url.startsWith("intent:", ignoreCase = true) ||
                url.startsWith("android-app:", ignoreCase = true)
            val intent: Intent = if (isIntentUri) {
                Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    // [deep-audit-1046 FIX-F9] A model-supplied intent URI is
                    // untrusted input: strip grant flags, clipData, a
                    // same-package component/package target and the injected
                    // selector. The sequence used to be spelled out here AND
                    // (in a much weaker form) in the browser handler; it now
                    // lives in one place so both call sites carry the same
                    // guards. See IntentSanitizer for what survives and why.
                    IntentSanitizer.sanitize(this, context)
                }
            } else {
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            val resolved = intent.resolveActivity(context.packageManager)
            Log.d(TAG, "startActivity url='$url' action=${intent.action} resolved=${resolved?.flattenToShortString()}")
            context.startActivity(intent)
            // [da1034-A4] ISSUE-08: JSON payload, not `Opened: <url>`. Sibling
            // error branches already carry `url`, so the success payload does too.
            val data = JSONObject().put("opened", true).put("url", url)
            NativeOffloadResult(0, OffloadOutput.formatBody(data.toString(2), args) + "\n")
        } catch (e: ActivityNotFoundException) {
            Log.w(TAG, "no handler for '$url'")
            maybeFallback(url, e, args, hop)
        } catch (e: SecurityException) {
            val body = JSONObject().put("error", "open_denied")
                .put("message", "The system refused to open this URL: ${e.message}")
                .put("url", url)
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, args) + "\n")
        } catch (e: java.net.URISyntaxException) {
            // T259: parseUri rejected a malformed intent: URI before any
            // resolveActivity call — surface as bad input, not no_handler,
            // so the model knows to fix the URI shape.
            Log.w(TAG, "parseUri failed for '$url': ${e.message}")
            val body = JSONObject().put("error", "bad_intent_uri")
                .put("message", "Malformed intent: URI: ${e.message}")
                .put("url", url)
                .toString()
            NativeOffloadResult(2, OffloadOutput.formatBody(body, args) + "\n")
        } catch (e: Throwable) {
            Log.w(TAG, "uncaught: ${e.message}", e)
            val body = JSONObject().put("error", "open_failed")
                .put("message", e.message ?: "Failed to open URL.")
                .put("url", url)
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, args) + "\n")
        }
    }

    /**
     * Fallback rules for URLs that couldn't be resolved. Returns a friendly
     * error message with a hint the model can relay to the user.
     */
    private fun maybeFallback(
        url: String,
        original: ActivityNotFoundException,
        args: OffloadArgs,
        hop: Int = 0,
    ): NativeOffloadResult {
        // market: → Huawei AppGallery on HMS-only devices.
        // [dv29-A3 A-07] The package name comes from [marketPackageOf], which
        // answers null when the link carries no `id`. The old
        // `getQueryParameter("id") ?: substringAfter("details?id=")` used the
        // receiver as the missing-delimiter default, so `market://search?q=foo`
        // produced `https://appgallery.huawei.com/app/market://search?q=foo`
        // and OPENED it. The redirect is also capped at one hop: the target is
        // an https URL, so a second fallback could only ever repeat the defect.
        if (url.startsWith("market://") && OsCompat.isHuawei && hop < MAX_FALLBACK_HOPS) {
            val pkg = marketPackageOf(url)
            if (pkg != null) {
                val ag = "https://appgallery.huawei.com/app/$pkg"
                Log.d(TAG, "market: redirect → AppGallery $ag")
                return tryOpen(ag, args, hop + 1)
            }
        }

        val schemeHint = when {
            url.startsWith("mailto:") -> "no email app installed"
            url.startsWith("tel:") -> "no phone/dialer app installed"
            url.startsWith("sms:") -> "no SMS app installed"
            url.startsWith("geo:") -> "no maps app installed"
            url.startsWith("market:") -> "no app store installed"
            url.startsWith("http:") || url.startsWith("https:") -> "no browser installed"
            else -> "no app can handle this URI scheme"
        }
        val body = JSONObject().put("error", "no_handler")
            .put("message", "Cannot open '$url': $schemeHint. Ask the user to install a compatible app.")
            .put("url", url)
            .put("detail", original.message ?: "ActivityNotFoundException")
            .toString()
        return NativeOffloadResult(1, OffloadOutput.formatBody(body, args) + "\n")
    }

    companion object {
        private const val TAG = "OpenOffload"

        /** One redirect only — see [maybeFallback]. */
        private const val MAX_FALLBACK_HOPS = 1
        private const val HELP = """android-open — open a URL or URI with the system handler

Usage:
  android-open <url>
  android-open --help

Supports https://, tel:, mailto:, geo:, market:, intent:, and any other
scheme a device app can handle. On Huawei devices without Play Store,
market:// URLs automatically fall back to AppGallery.
Errors are returned as JSON: {"error":"no_handler","message":"...","url":"..."}.
"""
    }
}

/**
 * [dv29-A3 A-07] The package name inside a `market://` link, or null when the
 * link carries none.
 *
 * `substringAfter(delimiter)` defaults `missingDelimiterValue` to its RECEIVER,
 * so the old `url.substringAfter("details?id=")` answered the whole URL for a
 * link that has no `id` — `market://search?q=foo` became a "package name", and
 * the Huawei fallback opened `.../app/market://search?q=foo`.
 *
 * Hand-parsed instead of `Uri.getQueryParameter` so the decision stays pure (no
 * Android framework) and testable off-device: a market URL's `id` is a plain
 * package name, which is the only value this handler accepts here.
 */
internal fun marketPackageOf(url: String): String? {
    if (!url.startsWith("market://")) return null
    val query = url.substringAfter('?', "")
    if (query.isEmpty()) return null
    for (pair in query.split('&')) {
        if (pair.substringBefore('=', "") != "id") continue
        return pair.substringAfter('=', "").trim().takeIf { it.isNotEmpty() }
    }
    return null
}
