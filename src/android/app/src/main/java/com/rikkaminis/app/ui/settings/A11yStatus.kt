package com.rikkaminis.app.ui.settings

/**
 * [dv15-B2 / deep-audit-1036 B-08] The accessibility state the permission
 * screens render, as one decision instead of two divergent copies.
 *
 * Deliberately dependency-free (no `Context`, no Android type) so the whole
 * input matrix is exercised by a JVM unit test — [classifyA11yStatus] is the
 * only place in the app that maps "what the OS reports" to "what the user is
 * told".
 *
 * The four inputs are the four independent observations available:
 *  - `masterSwitchOn`   — `Settings.Secure.ACCESSIBILITY_ENABLED` (absent ⇒ ON,
 *                          see [com.rikkaminis.app.accessibility.A11yState]);
 *  - `listedInSettings` — our service appears in the enabled-services list;
 *  - `serviceConnected` — the framework actually bound
 *                          [com.rikkaminis.app.accessibility.MinisAccessibilityService];
 *  - `everGranted`      — the authorisation was held at least once on this
 *                          install (latched preference), i.e. its later absence
 *                          is a revocation rather than a fresh install.
 *
 * Matrix (rows are the 16 combinations, collapsed by the branches below):
 *
 * | master | listed | bound | ever | status      | why                                        |
 * |--------|--------|-------|------|-------------|--------------------------------------------|
 * | off    | any    | any   | any  | NOT_ENABLED | bound but deaf — receives no events        |
 * | on     | any    | yes   | any  | READY       | the only state where tools really work     |
 * | on     | yes    | no    | any  | DEGRADED    | listed yet unbound: the ROM killed it      |
 * | on     | no     | no    | yes  | REVOKED     | grant gone after being held (force-stop)   |
 * | on     | no     | no    | no   | NOT_ENABLED | fresh install: nothing was ever granted    |
 *
 * Two properties this shape is built to keep:
 *  1. **DEGRADED is never READY.** The pre-fix live screen computed
 *     `isServiceEnabled(context) || getInstance() != null`, so a listed-but-killed
 *     service (`listedInSettings == true`, `serviceConnected == false`) reported
 *     "enabled", hid the settings entry point and never offered the two rescue
 *     cards that only the unreachable screen had.
 *  2. **The connected fallback survives.** A bound service is real evidence that
 *     events flow, so `serviceConnected` outranks the listing: a ROM that keeps
 *     the service bound without listing it the way [A11yState] expects still
 *     gets READY, exactly as the original `||` intended.
 */
internal enum class A11yStatus {
    /** Global switch on and our service bound — a11y tools receive events. */
    READY,

    /** Listed as enabled with the switch on, but nothing is bound: the ROM killed the process. */
    DEGRADED,

    /** The grant was held on this install and is now missing from Settings.Secure. */
    REVOKED,

    /** Never enabled, or accessibility is switched off system-wide. */
    NOT_ENABLED,
}

/**
 * Single source of truth for [A11yStatus].
 *
 * `listedInSettings` is fed `A11yState.isServiceEnabled(context)`, which already
 * ANDs the master switch in. That conflation is harmless *because*
 * `!masterSwitchOn` is decided first: the value of `listedInSettings` is only
 * ever consulted on rows where the switch is on, and there it equals the plain
 * listing.
 */
internal fun classifyA11yStatus(
    masterSwitchOn: Boolean,
    listedInSettings: Boolean,
    serviceConnected: Boolean,
    everGranted: Boolean,
): A11yStatus = when {
    // Bound but deaf: A11yState's KDoc records that the enabled-services list can
    // still name a service while accessibility is off entirely.
    !masterSwitchOn -> A11yStatus.NOT_ENABLED
    // Connected outranks the listing (the original `inSettings || connected`
    // fallback): the binder is the stronger evidence of a working service.
    serviceConnected -> A11yStatus.READY
    // Listed with the switch on yet unbound: the OEM killed the process. The
    // remediation is keep-alive guidance, NOT "turn it on again".
    listedInSettings -> A11yStatus.DEGRADED
    // Gone after being held — repairable by writing the setting back. A fresh
    // install must NOT land here (it would be shown a repair prompt for
    // something that was never broken).
    everGranted -> A11yStatus.REVOKED
    else -> A11yStatus.NOT_ENABLED
}
