package com.rikkaminis.app.config

import com.rikkaminis.app.config.ConfigRisk
import com.rikkaminis.app.config.confirm.ConfigAutoApprovePolicy
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [auto-approve] Pure decision table for skipping the minis-config
 * confirmation dialog. Expectations are hard-coded literals, never derived
 * from the production constants.
 */
class ConfigAutoApprovePolicyTest {

    @Test
    fun masterOffNeverSkips() {
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL), false, false))
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL), false, true))
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.SENSITIVE), false, true))
    }

    @Test
    fun allNormalSkipsWhenMasterOn() {
        assertTrue(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL), true, false))
        assertTrue(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL, ConfigRisk.NORMAL), true, false))
    }

    @Test
    fun sensitiveBlocksWhenSubFlagOff() {
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.SENSITIVE), true, false))
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL, ConfigRisk.SENSITIVE), true, false))
    }

    @Test
    fun sensitiveSkipsWhenSubFlagOn() {
        assertTrue(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.SENSITIVE), true, true))
        assertTrue(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL, ConfigRisk.SENSITIVE), true, true))
    }

    @Test
    fun destructiveNeverSkips() {
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.DESTRUCTIVE), true, true))
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.NORMAL, ConfigRisk.DESTRUCTIVE), true, true))
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(listOf(ConfigRisk.DESTRUCTIVE, ConfigRisk.SENSITIVE), true, true))
    }

    @Test
    fun emptyBatchNeverSkips() {
        assertFalse(ConfigAutoApprovePolicy.shouldSkip(emptyList(), true, true))
    }
}
