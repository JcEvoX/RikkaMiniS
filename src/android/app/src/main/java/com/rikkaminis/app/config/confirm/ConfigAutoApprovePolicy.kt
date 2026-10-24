package com.rikkaminis.app.config.confirm

import com.rikkaminis.app.config.ConfigRisk

/**
 * Pure decision for the "auto-approve" mode: may this batch of `minis-config`
 * writes skip the confirmation dialog entirely?
 *
 * Rules, in order:
 *  - master switch OFF            → never skip;
 *  - empty batch                  → never skip (nothing was actually resolved,
 *                                   so "approving" it would be meaningless);
 *  - any [ConfigRisk.DESTRUCTIVE] → never skip, at any flag combination;
 *  - any [ConfigRisk.SENSITIVE]   → skip only when the sensitive sub-flag is
 *                                   ON. Without it, a mixed batch is confirmed
 *                                   as a whole — partial skipping would leave
 *                                   the user unsure which rows they ever saw;
 *  - otherwise (all NORMAL)       → skip.
 *
 * Kept as a standalone object (no Android imports, no I/O) so the rule table
 * is unit-testable on the JVM. The single production caller is
 * `ConfigBridge.performWriteBatch`, which also owns the audit line written
 * when a batch is applied without a dialog.
 */
internal object ConfigAutoApprovePolicy {

    fun shouldSkip(
        risks: List<ConfigRisk>,
        autoApproveEnabled: Boolean,
        sensitiveAutoApproveEnabled: Boolean,
    ): Boolean {
        if (!autoApproveEnabled) return false
        if (risks.isEmpty()) return false
        if (risks.any { it == ConfigRisk.DESTRUCTIVE }) return false
        if (risks.any { it == ConfigRisk.SENSITIVE } && !sensitiveAutoApproveEnabled) return false
        return true
    }
}
