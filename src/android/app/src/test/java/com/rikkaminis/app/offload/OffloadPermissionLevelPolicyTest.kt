package com.rikkaminis.app.offload

import com.rikkaminis.app.offload.OffloadPermissionManager.PermissionLevel
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [auto-approve] Pure resolution of the effective permission level.
 * Expectations are hard-coded literals.
 */
class OffloadPermissionLevelPolicyTest {

    @Test
    fun storedLevelAlwaysWins() {
        // A user's explicit row choice must never be overridden by the mode —
        // including a stored NOT_ALLOWED with every flag armed.
        assertEquals(
            PermissionLevel.NOT_ALLOWED,
            OffloadPermissionLevelPolicy.resolve(
                storedLevel = PermissionLevel.NOT_ALLOWED,
                defaultLevel = PermissionLevel.ASK_ONCE,
                isPrivilegedTool = true,
                autoApproveEnabled = true,
                privilegedToolsEnabled = true,
            ),
        )
        assertEquals(
            PermissionLevel.ASK_ONCE,
            OffloadPermissionLevelPolicy.resolve(
                storedLevel = PermissionLevel.ASK_ONCE,
                defaultLevel = PermissionLevel.ASK_ONCE,
                isPrivilegedTool = false,
                autoApproveEnabled = true,
                privilegedToolsEnabled = true,
            ),
        )
    }

    @Test
    fun askOnceLiftsToBypassWhenMasterOn() {
        assertEquals(
            PermissionLevel.BYPASS,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.ASK_ONCE, false, true, false),
        )
    }

    @Test
    fun askOnceStaysWhenMasterOff() {
        assertEquals(
            PermissionLevel.ASK_ONCE,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.ASK_ONCE, false, false, false),
        )
    }

    @Test
    fun privilegedLiftsToBypassOnlyWithItsOwnFlag() {
        assertEquals(
            PermissionLevel.BYPASS,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.NOT_ALLOWED, true, false, true),
        )
        assertEquals(
            PermissionLevel.NOT_ALLOWED,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.NOT_ALLOWED, true, false, false),
        )
    }

    @Test
    fun nonPrivilegedNotAllowedNeverLifts() {
        // The master switch must not reach a non-privileged NOT_ALLOWED row —
        // only the dedicated privileged sub-flag can, and only for privileged
        // tools.
        assertEquals(
            PermissionLevel.NOT_ALLOWED,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.NOT_ALLOWED, false, true, true),
        )
        assertEquals(
            PermissionLevel.NOT_ALLOWED,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.NOT_ALLOWED, false, true, false),
        )
    }

    @Test
    fun bypassStaysBypass() {
        assertEquals(
            PermissionLevel.BYPASS,
            OffloadPermissionLevelPolicy.resolve(null, PermissionLevel.BYPASS, false, false, false),
        )
    }
}
