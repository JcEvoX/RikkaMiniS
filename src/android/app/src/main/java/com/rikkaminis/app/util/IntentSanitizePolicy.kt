package com.rikkaminis.app.util

/**
 * Android-free half of the `intent://` hardening shared by
 * [com.rikkaminis.app.sandbox.offload.OpenOffloadHandler] (`android-open`) and
 * [com.rikkaminis.app.ui.browser.BrowserExternalSchemeHandler] (scheme taps
 * inside the in-app WebViews).
 *
 * [deep-audit-1046 FIX-F9] The decisions live here, away from `android.content`
 * spelling, for two reasons: they are the part that has to be *pinned* (a
 * regression here silently re-opens a URI-grant escape), and a pure function
 * can be exercised by a plain JVM test instead of a device/emulator run.
 * [IntentSanitizer] is the thin Android translator.
 */
internal object IntentSanitizePolicy {

    /**
     * Whether [target] (a parsed `component=` / `package=` target) names THIS
     * app and must therefore be dropped before `startActivity`.
     *
     * A same-uid explicit intent reaches an `exported="false"` component — the
     * manifest never arbitrates same-uid launches — and `package=` pins
     * resolution to us exactly like a component does. A THIRD-PARTY target is
     * the feature (WeChat / Alipay / Play Store) and must survive verbatim.
     */
    fun isSelfTarget(target: String?, selfPackage: String): Boolean =
        !target.isNullOrBlank() && target == selfPackage

    /**
     * `browser_fallback_url` gate: the extra is page-supplied, so only an
     * ordinary web URL may become the second `startActivity`. Anything else
     * (`intent://`, `file://`, `content://`, a scheme-less string, a
     * protocol-relative `//host`) is dropped — the caller then shows the
     * "no app for this link" toast instead of firing an intent the page chose.
     */
    fun allowedFallbackUrl(raw: String?): String? {
        val candidate = raw?.trim().orEmpty()
        if (candidate.isEmpty()) return null
        val scheme = candidate.substringBefore(':', missingDelimiterValue = "").lowercase()
        return if (scheme == "http" || scheme == "https") candidate else null
    }
}

/**
 * Lower-case the URI SCHEME PREFIX only.
 *
 * `Intent.parseUri` matches `intent:` / `android-app:` case-sensitively inside
 * `parseUriInternal`, so a mixed-case scheme is silently degraded to a plain
 * ACTION_VIEW over the raw string. RFC 3986 declares schemes
 * case-insensitive, so normalising just the prefix is lossless: everything
 * from the first ':' onward — path, query and the `#Intent;…;end` fragment,
 * whose keys and values ARE case-sensitive — comes back byte-identical.
 *
 * [deep-audit-1041 FIX-A6] introduced this for `android-open`;
 * [deep-audit-1046 FIX-F9] moved it here so the browser path shares it instead
 * of parsing the page's raw casing.
 */
internal fun lowercaseIntentScheme(url: String): String {
    val sep = url.indexOf(':')
    if (sep <= 0) return url
    return url.substring(0, sep).lowercase() + url.substring(sep)
}
