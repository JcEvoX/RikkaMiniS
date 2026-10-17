package com.rikkaminis.app.config

import com.rikkaminis.app.config.collections.CUSTOM_USER_AGENT_OAUTH_MESSAGE
import com.rikkaminis.app.config.collections.customUserAgentWritable
import com.rikkaminis.app.config.collections.resolveAddCustomUserAgent
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.ui.chat.MEMORY_DISABLED_TOOL_TITLE
import com.rikkaminis.app.ui.chat.memoryToolDisabledMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * [fix/da1020-memory-gate-provider-fields] Covers the two pure pieces this
 * fix added:
 *  - the memory tool gate ([memoryToolDisabledMessage]) shared by the
 *    memory_write / memory_rollup executors, and
 *  - the provider `add` customUserAgent resolution
 *    ([resolveAddCustomUserAgent] + [customUserAgentWritable]), which reuses
 *    the per-instance write field's credential rule.
 *
 * The wiring (rollup executor + wrapper call sites, `add` →
 * ProviderInstance, ConfigBuiltins providers summary) is asserted by the
 * static gate `scripts/scan/memory_gate_provider_fields_check.py`; the
 * executor/bridge paths need a live ViewModel / registry + Context and are
 * exercised on-device via minis-config.
 *
 * Expected values are hardcoded literals (NOT derived from the constants
 * under test) so a constant change can't silently mask a regression.
 */
class MemoryGateAndProviderUaTest {

    // ── memory tool gate ─────────────────────────────────────────────

    @Test
    fun gate_disabled_returns_the_rejection_message() {
        // Same wording memory_write answered with before the fix; rollup
        // must answer identically (同形).
        assertEquals(
            "Memory writes are disabled for this session (user toggled /memory off). Reads remain available.",
            memoryToolDisabledMessage(false),
        )
    }

    @Test
    fun gate_enabled_returns_null() {
        assertNull(memoryToolDisabledMessage(true))
    }

    @Test
    fun disabled_tool_title_is_shared() {
        assertEquals("Memory (disabled)", MEMORY_DISABLED_TOOL_TITLE)
    }

    // ── provider add: customUserAgent ────────────────────────────────

    @Test
    fun ua_writable_for_api_key_only() {
        assertTrue(customUserAgentWritable(ProviderCredential.apiKey))
        assertEquals(false, customUserAgentWritable(ProviderCredential.oauth))
    }

    @Test
    fun add_ua_payload_resolves_verbatim_for_api_key() {
        assertEquals("MyAgent/1.0", resolveAddCustomUserAgent("MyAgent/1.0", ProviderCredential.apiKey))
    }

    @Test
    fun add_ua_absent_or_blank_resolves_to_null() {
        assertNull(resolveAddCustomUserAgent(null, ProviderCredential.apiKey))
        assertNull(resolveAddCustomUserAgent("", ProviderCredential.apiKey))
        assertNull(resolveAddCustomUserAgent("   ", ProviderCredential.apiKey))
        // Absent on an OAuth create must keep working (no override, no throw).
        assertNull(resolveAddCustomUserAgent(null, ProviderCredential.oauth))
        assertNull(resolveAddCustomUserAgent("", ProviderCredential.oauth))
    }

    @Test
    fun add_ua_on_oauth_credential_throws_the_write_field_wording() {
        try {
            resolveAddCustomUserAgent("MyAgent/1.0", ProviderCredential.oauth)
            fail("expected ConfigError.InvalidValue")
        } catch (e: ConfigError.InvalidValue) {
            // Hardcoded literal = the exact wording the per-instance write
            // field threw before this fix (invalid_value: prefix added by
            // the ConfigError envelope).
            assertEquals(
                "invalid_value: Custom User-Agent is only supported for third-party API-key providers. " +
                    "OAuth providers (Anthropic/Codex) use their own authentication UA and cannot be overridden.",
                e.message,
            )
            assertEquals(
                "invalid_value: $CUSTOM_USER_AGENT_OAUTH_MESSAGE",
                e.message,
            )
        }
    }
}
