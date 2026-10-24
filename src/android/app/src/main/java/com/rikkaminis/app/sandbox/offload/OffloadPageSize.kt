package com.rikkaminis.app.sandbox.offload

/**
 * [dv29-A3 A-05] The ONE page-size resolver for the offload CLIs.
 *
 * `1048-A06` closed this contract for `android-contacts` alone: `--max abc` and
 * `--max 0` are usage errors (exit 2), never a silent default and never an
 * empty page. Every other paginating handler kept the older shape
 * (`getInt("limit") ?: default`, `.coerceIn(1, …)`, `.coerceAtLeast(1)`), so
 * the same defects stayed reachable from the release CLI (all four verified
 * on-device, 2026-10-24, app 1.0.0+2597):
 *
 *  - `android-calendar list --limit -5` → exit 0, `"events": []`, and the
 *    warning `Results truncated by --limit. Returned -5 of 0 total records.`
 *    (`total > limit` is true for ANY total once the limit is negative, so even
 *    an empty window was announced as truncated);
 *  - `android-photos list --limit 0` / `--limit -1` → one row (clamped to 1);
 *    `--limit abc` → the default 20; `--limit 100000` → silently 100;
 *  - `minis-sessions-cli list --limit 0` → 1 row; `--limit 1000` → 100 rows;
 *    `--limit abc` → 50;
 *  - `android-notification list --max 0` → 1 of 11 active notifications;
 *    `--max abc` → all 11.
 *
 * A clamped or defaulted page answers a `count` the caller cannot question:
 * "you asked for 0 and I gave you 1" is indistinguishable from "there is 1".
 * That is the defect — the number is a preference here, and every surface below
 * documents a page size as a request the handler either honours or refuses.
 *
 * The contract, unchanged from `1048-A06` and now shared: three outcomes — a
 * spelled value inside `1..max`, a usage error (exit 2) for anything else, or
 * the documented default when nothing was spelled at all. Out-of-range is
 * REFUSED rather than clamped, for the same reason: a clamped page reports
 * success for a request the handler did not honour.
 *
 * [OffloadPageSize.SURFACES] is the single source the static gate reads: it
 * derives its scope from [PageSizeSurface.handlerFile], so a handler cannot
 * adopt this contract without declaring where it lives, and a declared surface
 * that is missing from [OffloadPageSize.SURFACES] is a red gate.
 *
 * Pure — no Context, no I/O — so every arm stays testable off-device.
 */

/**
 * One CLI surface's page-size contract.
 *
 * @param handlerFile the handler file that owns this contract. The static gate
 *   (`scripts/scan/offload_cli_args_check.py`) derives its scope from these
 *   entries instead of a hand-written file list, so this field is load-bearing
 *   documentation, not a label.
 * @param names long spellings accepted, in precedence order: the first one
 *   PRESENT wins (`--limit` over `--max`, the apple-calendar naming rule), but
 *   every present spelling is validated, so `--limit 10 --max abc` refuses
 *   instead of silently discarding the garbage.
 * @param default answered only when no spelling is present at all.
 * @param max inclusive upper bound, or null when the surface has no natural cap
 *   (a window-bounded calendar listing, a device-wide notification list).
 * @param legacyShortMax whether the legacy `-n N` short form is rewritten to
 *   `--max N` before the parse (contacts). A `-n` that survives the rewrite
 *   carries no number and is refused here.
 */
internal class PageSizeSurface(
    val handlerFile: String,
    val names: List<String>,
    val default: Int,
    val max: Int?,
    val legacyShortMax: Boolean = false,
) {
    /** The accepted range, spelled the same way in every refusal of this surface. */
    val range: String
        get() = if (max == null) "an integer >= 1" else "an integer between 1 and $max"
}

/**
 * The outcome of resolving one page size.
 *
 * A plain `Int` cannot express the difference the defects depended on:
 * `--max abc` and "no `--max` at all" both arrive as `null` out of `getInt`,
 * and a non-positive value used to be handed straight to a `rows.size < max`
 * loop, which answered an empty page at exit 0.
 */
internal sealed class PageSizeOutcome {
    /** A usable page size, always within the surface's accepted range. */
    data class Ok(val value: Int) : PageSizeOutcome()

    /** The caller spelled a page size that cannot be honoured. */
    data class Invalid(val message: String) : PageSizeOutcome()

    companion object {
        /**
         * Resolve [surface]'s page size from [args].
         *
         * `default` is a parameter (not read from [surface]) so one surface can
         * carry two documented defaults — `android-contacts list` answers 50,
         * `search` answers 20 — without a second registry entry that the gate
         * would have to reconcile.
         */
        fun resolve(
            args: OffloadArgs,
            surface: PageSizeSurface,
            default: Int = surface.default,
        ): PageSizeOutcome {
            // Every PRESENT spelling is read as the RAW string and validated:
            // `getInt` maps "spelled but unreadable" onto "not spelled", which
            // is exactly how `--limit abc` used to answer the default.
            var winner: Ok? = null
            for (name in surface.names) {
                val raw = args.get(name) ?: continue
                when (val parsed = checked(raw, surface, "--$name")) {
                    is Invalid -> return parsed
                    is Ok -> if (winner == null) winner = parsed
                }
            }
            // A spelling with no follower binds no value at all — OffloadArgs
            // files the key as a boolean flag — so its PRESENCE has to be read
            // explicitly, or it silently answers the default.
            for (name in surface.names) {
                if (args.hasFlag(name)) return Invalid("--$name expects ${surface.range}")
            }
            if (surface.legacyShortMax && args.hasFlag("n")) {
                return Invalid(
                    "-n expects ${surface.range} (short form of --${surface.names.first()})",
                )
            }
            return winner ?: Ok(default)
        }

        /** Parse + range-check one spelled page size. Pure. */
        private fun checked(raw: String, surface: PageSizeSurface, spelled: String): PageSizeOutcome {
            val value = raw.toIntOrNull()
                ?: return Invalid("$spelled expects ${surface.range}, got '$raw'")
            if (value < 1 || (surface.max != null && value > surface.max)) {
                return Invalid("$spelled expects ${surface.range}, got $value")
            }
            return Ok(value)
        }
    }
}

/**
 * The registered surfaces — the single source of truth for "this CLI has a page
 * size, and this is the handler that owns it".
 *
 * Kept as one object rather than one constant per handler so that adding a
 * paginating subcommand is a one-file change here plus one call site, and so
 * that the gate's scope is derived (see [PageSizeSurface.handlerFile]).
 */
internal object OffloadPageSize {
    /** `android-contacts list` (50) / `search` (20) — the `1048-A06` contract. */
    val CONTACTS = PageSizeSurface(
        handlerFile = "ContactsOffloadHandler.kt",
        names = listOf("max"),
        default = 50,
        max = 500,
        legacyShortMax = true,
    )

    /**
     * `android-calendar` (list / the default subcommand). No upper bound: the
     * result set is already window-bounded (`--today` / `--days` / `--start`),
     * the two-pass list reports `total_available`, and an artificial cap here
     * would refuse a request the handler can honour.
     */
    val CALENDAR = PageSizeSurface(
        handlerFile = "CalendarOffloadHandler.kt",
        names = listOf("limit", "max"),
        default = 50,
        max = null,
    )

    /** `android-photos list` / `near` — a MediaStore page per media class. */
    val PHOTOS = PageSizeSurface(
        handlerFile = "PhotosOffloadHandler.kt",
        names = listOf("limit", "max"),
        default = 20,
        max = 100,
    )

    /** `android-photos album` — the same page, twice the budget (documented 200). */
    val PHOTOS_ALBUM = PageSizeSurface(
        handlerFile = "PhotosOffloadHandler.kt",
        names = listOf("limit", "max"),
        default = 50,
        max = 200,
    )

    /**
     * `android-notification list`. No upper bound: the list is the device's
     * active notifications, already a small, self-limiting set.
     */
    val NOTIFICATION = PageSizeSurface(
        handlerFile = "NotificationOffloadHandler.kt",
        names = listOf("max"),
        default = 50,
        max = null,
    )

    /** `minis-sessions-cli list|search|messages` — documented "default: 50, max: 100". */
    val SESSIONS = PageSizeSurface(
        handlerFile = "SessionsOffloadHandler.kt",
        names = listOf("limit"),
        default = 50,
        max = 100,
    )

    /** Every surface above. A declared surface missing here is a red gate. */
    val SURFACES = listOf(CONTACTS, CALENDAR, PHOTOS, PHOTOS_ALBUM, NOTIFICATION, SESSIONS)
}
