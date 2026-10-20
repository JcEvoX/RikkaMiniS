package com.rikkaminis.app.network

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * JVM tests for [NetworkStateMachine] — the pure transition table behind
 * NetworkMonitor's callbacks ([dv15-A4] deep-audit-1036 A-06).
 *
 * The machine is deliberately free of android.* types, so the two pre-fix
 * asymmetries are exercised here directly instead of on a device:
 *
 *  - `onAvailable` used to evict only on a DISCONNECTED → CONNECTED flip, so a
 *    Make-Before-Break swap (new interface up while the old one is still the
 *    default, then the old one dies) never evicted the pools;
 *  - `onLost` used to write DISCONNECTED unconditionally, so losing a
 *    *secondary* interface looked like going offline on a live device.
 *
 * Expectation values are literals, never derived from production constants, so
 * a change that silently alters the table shows up as a red test rather than a
 * self-fulfilling pass.
 */
class NetworkStateMachineTest {

    /** Stand-in for `android.net.Network` — the machine only needs identity. */
    private class Net(private val name: String) {
        override fun toString(): String = name
    }

    private val wifi = Net("wifi")
    private val cell = Net("cell")

    @Test
    fun seedOnline_isTheStateThePoolsWereBuiltIn() {
        val machine = NetworkStateMachine<Net>()

        val seeded = machine.seed(wifi, online = true)

        assertEquals(true, seeded.online)
        assertEquals(false, seeded.changed)
        assertEquals(false, seeded.evictPool)
        assertEquals(false, seeded.warmPool)
        assertEquals(1, machine.liveCount())

        // The first callback for the SAME network must not be read as a move.
        val again = machine.onAvailable(wifi, wifi)

        assertEquals(true, again.online)
        assertEquals(false, again.changed)
        assertEquals(false, again.evictPool)
        assertEquals(false, again.warmPool)
    }

    @Test
    fun seedOffline_firstAvailableEvictsAndWarms() {
        val machine = NetworkStateMachine<Net>()

        val seeded = machine.seed(null, online = false)

        assertEquals(false, seeded.online)
        assertEquals(0, machine.liveCount())

        val arrived = machine.onAvailable(wifi, wifi)

        assertEquals(true, arrived.online)
        assertEquals(true, arrived.changed)
        assertEquals(true, arrived.evictPool)
        assertEquals(true, arrived.warmPool)
    }

    @Test
    fun newInterfaceWhileDefaultUnchanged_doesNotChurnThePools() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)

        val arrived = machine.onAvailable(cell, wifi)

        assertEquals(true, arrived.online)
        assertEquals(false, arrived.changed)
        assertEquals(false, arrived.evictPool)
        assertEquals(false, arrived.warmPool)
        assertEquals(2, machine.liveCount())
    }

    @Test
    fun makeBeforeBreak_defaultMovesToNewInterface_evictsAndWarms() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        // Android already hands the new interface over as default when the
        // callback lands — the pools still hold wifi sockets.
        val moved = machine.onAvailable(cell, cell)

        assertEquals(true, moved.online)
        assertEquals(false, moved.changed)
        assertEquals(true, moved.evictPool)
        assertEquals(true, moved.warmPool)
    }

    @Test
    fun makeBeforeBreak_oldInterfaceDies_verdictStaysOnline_poolsEvicted() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        val lost = machine.onLost(wifi, cell)

        // The interface the pools were built on left the usable set.
        assertEquals(true, lost.online)
        assertEquals(false, lost.changed)
        assertEquals(true, lost.evictPool)
        assertEquals(true, lost.warmPool)
        assertEquals(1, machine.liveCount())
    }

    @Test
    fun poolInterfaceLostBeforeDefaultMoves_stillEvicts() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        // onLost can land before the default has switched over: the pools are
        // still bound to the interface that just went away.
        val lost = machine.onLost(wifi, wifi)

        assertEquals(true, lost.online)
        assertEquals(false, lost.changed)
        assertEquals(true, lost.evictPool)
        assertEquals(true, lost.warmPool)
    }

    @Test
    fun secondaryInterfaceLost_keepsVerdictOnline_withoutChurn() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        val lost = machine.onLost(cell, wifi)

        assertEquals(true, lost.online)
        assertEquals(false, lost.changed)
        assertEquals(false, lost.evictPool)
        assertEquals(false, lost.warmPool)
    }

    @Test
    fun lastNetworkLost_goesOffline_andEvicts() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)

        val lost = machine.onLost(wifi, null)

        assertEquals(false, lost.online)
        assertEquals(true, lost.changed)
        assertEquals(true, lost.evictPool)
        assertEquals(false, lost.warmPool)
        assertEquals(0, machine.liveCount())
    }

    @Test
    fun twoNetworksLostInSequence_onlyTheLastOneIsTheOfflineFlip() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        val first = machine.onLost(cell, wifi)
        assertEquals(true, first.online)
        assertEquals(false, first.changed)

        val second = machine.onLost(wifi, null)
        assertEquals(false, second.online)
        assertEquals(true, second.changed)
        assertEquals(true, second.evictPool)
    }

    @Test
    fun captivePortal_capabilityLost_goesOffline() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)

        val portal = machine.onCapabilitiesChanged(wifi, hasInternet = false, defaultKey = wifi)

        assertEquals(false, portal.online)
        assertEquals(true, portal.changed)
        assertEquals(true, portal.evictPool)
        assertEquals(false, portal.warmPool)
    }

    @Test
    fun capabilitiesReConfirmed_doNotChurnThePools() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)

        val confirmed = machine.onCapabilitiesChanged(wifi, hasInternet = true, defaultKey = wifi)

        assertEquals(true, confirmed.online)
        assertEquals(false, confirmed.changed)
        assertEquals(false, confirmed.evictPool)
        assertEquals(false, confirmed.warmPool)
    }

    @Test
    fun secondaryInterfaceLosingInternet_doesNotChurnAHealthyDefault() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onAvailable(cell, wifi)

        val portal = machine.onCapabilitiesChanged(cell, hasInternet = false, defaultKey = wifi)

        assertEquals(true, portal.online)
        assertEquals(false, portal.changed)
        assertEquals(false, portal.evictPool)
        assertEquals(false, portal.warmPool)
        assertEquals(1, machine.liveCount())
    }

    @Test
    fun recoveryAfterOffline_warmsThePool() {
        val machine = NetworkStateMachine<Net>()
        machine.seed(wifi, online = true)
        machine.onLost(wifi, null)

        val back = machine.onAvailable(wifi, wifi)

        assertEquals(true, back.online)
        assertEquals(true, back.changed)
        assertEquals(true, back.evictPool)
        assertEquals(true, back.warmPool)
    }
}
