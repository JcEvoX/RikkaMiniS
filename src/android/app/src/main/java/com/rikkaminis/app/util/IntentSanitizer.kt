package com.rikkaminis.app.util

import android.content.Context
import android.content.Intent

/**
 * Shared hardening for intents built out of an `intent://` / `android-app://`
 * URL, i.e. from a PAGE or a MODEL, never from us.
 *
 * [deep-audit-1046 FIX-F9] Two call sites need the exact same sequence:
 * [com.rikkaminis.app.sandbox.offload.OpenOffloadHandler] for `android-open`
 * and [com.rikkaminis.app.ui.browser.BrowserExternalSchemeHandler] for scheme
 * taps inside the in-app WebViews. The browser side used to strip the selector
 * ONLY, so a web page could hand a launched activity a read grant on our
 * FileProvider roots (`minis-global/`, `minis-sessions/`, `shared/`), smuggle
 * URIs along via `clipData`, or start a non-exported activity of ours through a
 * same-package `component`/`package` target. One contract, two implementations,
 * only one of them guarded — that is the defect class, so the sequence lives
 * here once and the gate derives both call sites.
 */
internal object IntentSanitizer {

    /**
     * Strip everything a crafted `intent://` could use to reach into THIS app
     * or hand out URI grants, then return the intent still dispatchable.
     *
     * Kept on purpose: a THIRD-PARTY `component` / `package` target — launching
     * WeChat / Alipay / the Play Store by package is the feature, and
     * `FLAG_ACTIVITY_NEW_TASK` / `CATEGORY_BROWSABLE`, which the callers add.
     *
     * Removed:
     *   • the four URI-grant flags — otherwise the page grants read/write (or
     *     persistable) access to whatever URI it named;
     *   • `clipData` — carries attacker-chosen URIs and their grants along;
     *   • a same-package `component` — `exported="false"` does not arbitrate a
     *     same-uid explicit intent, so the manifest never gets a say;
     *   • a same-package `package=` target — `Intent.parseUri` writes it into
     *     `getPackage()`, which pins resolution to us exactly like a component;
     *   • the browser-injected `selector`.
     */
    fun sanitize(intent: Intent, context: Context): Intent {
        intent.removeFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                Intent.FLAG_GRANT_PREFIX_URI_PERMISSION,
        )
        intent.clipData = null
        val componentPackage = intent.component?.packageName
        if (IntentSanitizePolicy.isSelfTarget(componentPackage, context.packageName)) intent.component = null
        val pinnedPackage = intent.getPackage()
        if (IntentSanitizePolicy.isSelfTarget(pinnedPackage, context.packageName)) intent.setPackage(null)
        intent.selector = null
        return intent
    }
}
