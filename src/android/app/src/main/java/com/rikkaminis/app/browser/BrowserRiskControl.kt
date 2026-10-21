package com.rikkaminis.app.browser

import java.net.URI
import java.util.Locale

/**
 * Detects browser risk challenges (Cloudflare, captcha, rate limits, etc.)
 * and computes throttle delays to avoid triggering anti-bot measures.
 * Ported from OmniBot upstream.
 */
internal data class BrowserRiskChallenge(
    val kind: String,
    val recommendedNextAction: String
)

internal object BrowserRiskControl {
    private val searchHostPatterns = listOf(
        Regex("(^|\\.)google\\.[a-z.]+$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)bing\\.com$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)duckduckgo\\.com$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)yahoo\\.[a-z.]+$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)baidu\\.com$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)yandex\\.[a-z.]+$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)ecosia\\.org$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)search\\.brave\\.com$", RegexOption.IGNORE_CASE),
        Regex("(^|\\.)sogou\\.com$", RegexOption.IGNORE_CASE),
    )

    fun normalizedHost(rawUrl: String?): String? {
        val value = rawUrl?.trim().orEmpty()
        if (value.isBlank()) return null
        return runCatching {
            val uri = URI(value)
            uri.host
                ?.lowercase(Locale.ROOT)
                ?.removePrefix("www.")
                ?.takeIf { it.isNotBlank() }
        }.getOrNull()
    }

    fun isSearchHost(rawUrl: String?): Boolean {
        val host = normalizedHost(rawUrl) ?: return false
        return searchHostPatterns.any { pattern -> pattern.matches(host) }
    }

    /**
     * Returns true for actions that visually change the page and should
     * be throttled between consecutive calls.
     */
    fun shouldThrottle(action: BrowserAction): Boolean {
        return action == BrowserAction.NAVIGATE ||
            action == BrowserAction.CLICK ||
            action == BrowserAction.TYPE ||
            action == BrowserAction.SCROLL_AND_COLLECT
    }

    /**
     * Base delay (ms) for an action type before any search-host bonus.
     * Returns 0 for non-throttled actions.
     */
    fun baseThrottleDelayMs(
        action: BrowserAction,
        rawUrl: String?
    ): Long {
        val base = when (action) {
            BrowserAction.NAVIGATE -> 550L
            BrowserAction.CLICK -> 180L
            BrowserAction.TYPE -> 260L
            BrowserAction.SCROLL_AND_COLLECT -> 320L
            else -> 0L
        }
        if (base <= 0L || !isSearchHost(rawUrl)) {
            return base
        }
        val searchBonus = when (action) {
            BrowserAction.NAVIGATE -> 900L
            BrowserAction.CLICK -> 220L
            BrowserAction.TYPE -> 260L
            BrowserAction.SCROLL_AND_COLLECT -> 360L
            else -> 0L
        }
        return base + searchBonus
    }

    /**
     * Computes the actual throttle delay: the deficit since last action
     * (if any) plus jitter. If enough time has already elapsed since the
     * last action, returns only the jitter (or 0).
     *
     * @param baseDelayMs the base delay for this action type (from [baseThrottleDelayMs])
     * @param elapsedSinceLastActionMs time since the last throttled action, or null
     * @param jitterMs extra random/variable delay to add
     */
    fun computeThrottleDelayMs(
        baseDelayMs: Long,
        elapsedSinceLastActionMs: Long?,
        jitterMs: Long
    ): Long {
        if (baseDelayMs <= 0L) return 0L
        val normalizedJitter = jitterMs.coerceAtLeast(0L)
        // When elapsedSinceLastActionMs is null (no previous action tracked),
        // apply the full base delay. When it's non-null, compute the deficit
        // (base minus elapsed, floor 0) and add jitter.
        val deficit = if (elapsedSinceLastActionMs != null) {
            (baseDelayMs - elapsedSinceLastActionMs).coerceAtLeast(0L)
        } else {
            baseDelayMs
        }
        return deficit + normalizedJitter
    }

    /**
     * Detects a browser risk challenge from page metadata.
     * Pure function with no side effects — all parameters are nullable so
     * callers can pass whatever is available without extra page evaluation.
     *
     * @param statusCode HTTP status code (e.g. 429, 403), or null
     * @param title page document title, or null
     * @param bodyText page body text (first ~1000 chars is enough), or null
     * @param currentUrl the page URL, or null
     * @return a [BrowserRiskChallenge] if detected, null otherwise
     */
    fun detectChallenge(
        statusCode: Int? = null,
        title: String? = null,
        bodyText: String? = null,
        currentUrl: String? = null
    ): BrowserRiskChallenge? {
        // Status code-based detection (fast path, no text needed)
        when (statusCode) {
            429 -> return BrowserRiskChallenge(
                kind = "rate_limited",
                recommendedNextAction = "wait_before_retrying_and_reduce_request_rate"
            )
            403 -> return BrowserRiskChallenge(
                kind = "access_denied",
                recommendedNextAction = "stop_automatic_retry_and_use_manual_access"
            )
        }

        // Text-based detection
        val haystack = listOfNotNull(title, bodyText, currentUrl)
            .joinToString(" ")
            .replace(Regex("\\s+"), " ")
            .lowercase(Locale.ROOT)
        if (haystack.isBlank()) return null

        val searchHost = isSearchHost(currentUrl)
        return when {
            "cloudflare" in haystack &&
                ("challenge" in haystack || "attention required" in haystack) ->
                BrowserRiskChallenge(
                    kind = "cloudflare_challenge",
                    recommendedNextAction = "ask_user_to_complete_verification_manually"
                )

            searchHost &&
                ("unusual traffic" in haystack ||
                    "our systems have detected unusual traffic" in haystack ||
                    "automated queries" in haystack) ->
                BrowserRiskChallenge(
                    kind = "search_engine_challenge",
                    recommendedNextAction = "ask_user_to_complete_verification_manually"
                )

            "recaptcha" in haystack ||
                "hcaptcha" in haystack ||
                "turnstile" in haystack ||
                "captcha" in haystack ||
                "verify you are human" in haystack ||
                "security check" in haystack ->
                BrowserRiskChallenge(
                    kind = "captcha_challenge",
                    recommendedNextAction = "ask_user_to_complete_verification_manually"
                )

            "too many requests" in haystack || "rate limit" in haystack ->
                BrowserRiskChallenge(
                    kind = "rate_limited",
                    recommendedNextAction = "wait_before_retrying_and_reduce_request_rate"
                )

            "access denied" in haystack || "forbidden" in haystack ->
                BrowserRiskChallenge(
                    kind = "access_denied",
                    recommendedNextAction = "stop_automatic_retry_and_use_manual_access"
                )

            else -> null
        }
    }

    /**
     * [fix/dv17-1 B-01] Renders a detected challenge as one line the agent can
     * actually read.
     *
     * The four structured fields this object feeds into [BrowserActionResult]
     * (riskChallengeDetected / riskChallengeKind / recommendedNextAction, plus
     * the throttle) had **no reader anywhere in the tree** — `ChatToolExecutors`
     * consumes `result.text` and nothing else. So a Cloudflare challenge /
     * captcha / 429 reached the model as undistinguished page text, and it
     * retried blindly instead of waiting or handing over to the user. This is
     * the text-side exit; the fields stay written for other readers.
     *
     * Pure and Android-free (JVM-testable). Returns null when either half is
     * blank, so callers append **nothing** when no challenge was detected —
     * a `[risk]` line on every browser result would be pure noise for the
     * model and would blunt the signal on the runs that matter.
     *
     * @param kind machine-readable challenge kind (e.g. "cloudflare_challenge")
     * @param recommendedNextAction the action id the agent should take next
     * @param throttleDelayMs delay applied before this result returned; 0 when
     *   the action was not throttled. Surfaced only when positive.
     */
    fun challengeNoticeLine(
        kind: String?,
        recommendedNextAction: String?,
        throttleDelayMs: Long
    ): String? {
        val normalizedKind = kind?.trim().orEmpty()
        val action = recommendedNextAction?.trim().orEmpty()
        if (normalizedKind.isEmpty() || action.isEmpty()) return null
        val throttleSuffix = if (throttleDelayMs > 0L) " (throttled ${throttleDelayMs}ms)" else ""
        return "[risk] $normalizedKind — next: $action$throttleSuffix"
    }
}