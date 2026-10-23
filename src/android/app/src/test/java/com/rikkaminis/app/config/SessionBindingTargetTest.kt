package com.rikkaminis.app.config

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [dv25-A05 / dv25-A06] — the pure decisions behind the two RPC-facing session
 * configuration surfaces.
 *
 * Both defects are the same shape: a rule that exists elsewhere in the app was
 * not applied on the RPC / config face.
 *
 *  - A-05 `HeadlessChatRunner.applyModelOverride` applied the *new-session*
 *    default group unconditionally when the caller passed no model, which is
 *    exactly what `chat.prompt` / `chat.retry` pass when nobody asked for one
 *    — so a follow-up prompt silently rebound a pinned session.
 *  - A-06 the `session.*` readers only read the Room row while the writers also
 *    push into the resident `ChatViewModel`, so a write reported success and a
 *    read reported a different (for a draft: the empty) value.
 *
 * The decisions are Android-free on purpose (they live in
 * `SessionConfigWriteThrough.kt`, next to `route`), so they can be compiled and
 * pinned here without a device. The *wiring* is pinned separately, on the real
 * source text, by `SessionConfigReadThroughTest`.
 */
class SessionBindingTargetTest {

    // ── A-05: which binding an RPC model override writes ────────────────────

    @Test
    fun `an explicit entry request always wins`() {
        assertEquals(
            BindingTarget.Entry("entry-1"),
            resolveBindingTarget(
                requestedEntryId = "entry-1",
                requestedGroupId = null,
                existingBinding = """{"type":"group","groupId":"g0"}""",
                defaultGroupId = "g-default",
            ),
        )
    }

    @Test
    fun `an explicit group request always wins`() {
        assertEquals(
            BindingTarget.Group("g-asked"),
            resolveBindingTarget(
                requestedEntryId = null,
                requestedGroupId = "g-asked",
                existingBinding = """{"type":"entry","entryId":"pinned"}""",
                defaultGroupId = "g-default",
            ),
        )
    }

    /**
     * THE defect: `chat.prompt` without `modelEntryId` / `modelGroupId` used to
     * be treated as "no model picked, apply the default group". On a session
     * that already has a binding that is a silent rebind, not an override.
     */
    @Test
    fun `no request on an already bound session preserves the binding`() {
        assertEquals(
            BindingTarget.Preserve,
            resolveBindingTarget(
                requestedEntryId = null,
                requestedGroupId = null,
                existingBinding = """{"type":"entry","entryId":"user-pinned"}""",
                defaultGroupId = "g-default",
            ),
        )
    }

    @Test
    fun `the default group still applies to a session with no binding`() {
        assertEquals(
            BindingTarget.Group("g-default"),
            resolveBindingTarget(
                requestedEntryId = null,
                requestedGroupId = null,
                existingBinding = null,
                defaultGroupId = "g-default",
            ),
        )
    }

    @Test
    fun `an empty binding column counts as no binding`() {
        // `updateSessionBinding(sid, "", "")` is how both the UI and
        // `minis-config set session.primaryModel ""` clear a pin; a cleared
        // session must take the default group like a fresh one, not be treated
        // as "pinned to nothing".
        for (cleared in listOf("", "   ")) {
            assertEquals(
                BindingTarget.Group("g-default"),
                resolveBindingTarget(
                    requestedEntryId = null,
                    requestedGroupId = null,
                    existingBinding = cleared,
                    defaultGroupId = "g-default",
                ),
            )
        }
    }

    @Test
    fun `no request, no binding, no default group writes nothing`() {
        assertEquals(
            BindingTarget.None,
            resolveBindingTarget(
                requestedEntryId = null,
                requestedGroupId = null,
                existingBinding = null,
                defaultGroupId = null,
            ),
        )
    }

    @Test
    fun `an explicit empty request id is not a request`() {
        // The RPC decodes `optString(...).ifEmpty { null }`, but the decision is
        // the last line of defence: "" must never become `entry:` / `group:` in
        // a row that restoreFromBinding then refuses.
        assertEquals(
            BindingTarget.Preserve,
            resolveBindingTarget(
                requestedEntryId = "",
                requestedGroupId = "",
                existingBinding = """{"type":"entry","entryId":"user-pinned"}""",
                defaultGroupId = "g-default",
            ),
        )
    }

    // ── A-06: which surface a `session.*` reader answers from ───────────────

    @Test
    fun `thinking level prefers the resident value once it adopted the row`() {
        assertEquals(
            SessionConfigReadSource.ResidentViewModel,
            thinkingLevelReadSource(
                hasLiveViewModel = true,
                vmAdoptedRow = true,
                hasPersistedRow = true,
            ),
        )
    }

    @Test
    fun `an unloaded resident vm defers to the row`() {
        // Mid-`loadSession()` the VM still holds the cross-conversation seed,
        // which says nothing about THIS session.
        assertEquals(
            SessionConfigReadSource.PersistedRow,
            thinkingLevelReadSource(
                hasLiveViewModel = true,
                vmAdoptedRow = false,
                hasPersistedRow = true,
            ),
        )
    }

    @Test
    fun `a draft answers from the vm even before it adopts anything`() {
        // The writer's draft case: no row existed, so the VM is the only place
        // the value landed. This is the asymmetry A-06 reported.
        assertEquals(
            SessionConfigReadSource.ResidentViewModel,
            thinkingLevelReadSource(
                hasLiveViewModel = true,
                vmAdoptedRow = false,
                hasPersistedRow = false,
            ),
        )
        assertEquals(
            SessionConfigReadSource.Absent,
            thinkingLevelReadSource(
                hasLiveViewModel = false,
                vmAdoptedRow = false,
                hasPersistedRow = false,
            ),
        )
    }

    @Test
    fun `the model binding answers from the row whenever it exists`() {
        // Row = the EXPLICIT pin; VM = the model in use (also non-empty on the
        // loadSession fallback path). Reading the VM here would invent a pin.
        assertEquals(
            SessionConfigReadSource.PersistedRow,
            modelBindingReadSource(hasLiveViewModel = true, hasPersistedRow = true),
        )
        assertEquals(
            SessionConfigReadSource.ResidentViewModel,
            modelBindingReadSource(hasLiveViewModel = true, hasPersistedRow = false),
        )
        assertEquals(
            SessionConfigReadSource.Absent,
            modelBindingReadSource(hasLiveViewModel = false, hasPersistedRow = false),
        )
    }
}
