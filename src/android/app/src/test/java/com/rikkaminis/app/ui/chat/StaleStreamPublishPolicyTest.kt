package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/cancel-stale-publish] Tests for [StaleStreamPublishPolicy] — the
 * admission check that keeps a late `isStreaming = true` side-channel publish
 * from re-minting an overlay entry for a message row the interrupt path
 * already finalized.
 *
 * The field symptom: interrupting the model while it is executing a tool
 * (sendMessage → enqueuePrompt + cancelStream) sometimes makes the tool
 * capsules disappear (or flip back to a ghost "running") — the canonical row
 * holds the CANCELLED capsules, but `mergeStreamingOverlay` REPLACES the row's
 * blocks wholesale from whatever side-channel entry exists, and the loop job's
 * cooperative cancel does not cover the per-line `onBlockUpdate` publishes, the
 * already-dispatched Main publishes, or the throttle trailing job.
 */
class StaleStreamPublishPolicyTest {

    // ── 活跃回合并发：放行 ─────────────────────────────────────────────
    @Test
    fun `live streaming row admits the publish`() {
        assertEquals(
            StaleStreamPublishPolicy.Verdict.PUBLISH,
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = true,
                hasSideChannelEntry = false,
            ),
        )
    }

    @Test
    fun `live streaming row with existing entry admits the publish`() {
        // The normal steady state of a chatty tool: entry present, ticks keep
        // replacing it.
        assertEquals(
            StaleStreamPublishPolicy.Verdict.PUBLISH,
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = true,
                hasSideChannelEntry = true,
            ),
        )
    }

    // ── 已定稿行 + 无 entry：丢弃（本次修复的核心） ────────────────────
    @Test
    fun `finalized row without side-channel entry drops the publish`() {
        assertEquals(
            StaleStreamPublishPolicy.Verdict.DROP_FINALIZED,
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = false,
                hasSideChannelEntry = false,
            ),
        )
    }

    @Test
    fun `finalized row WITH a surviving entry stays fail-open`() {
        // Safety valve: if a live entry somehow outlives the row's flags, the
        // entry is the fresher state and today's behaviour is preserved.
        assertEquals(
            StaleStreamPublishPolicy.Verdict.PUBLISH,
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = false,
                hasSideChannelEntry = true,
            ),
        )
    }

    // ── 行不存在：放行（overlay 对未知 id 是惰性的） ───────────────────
    @Test
    fun `missing row admits the publish`() {
        assertEquals(
            StaleStreamPublishPolicy.Verdict.PUBLISH,
            StaleStreamPublishPolicy.verdict(
                rowExists = false,
                rowIsStreaming = false,
                hasSideChannelEntry = false,
            ),
        )
    }

    @Test
    fun `missing row with stale entry admits the publish`() {
        assertEquals(
            StaleStreamPublishPolicy.Verdict.PUBLISH,
            StaleStreamPublishPolicy.verdict(
                rowExists = false,
                rowIsStreaming = false,
                hasSideChannelEntry = true,
            ),
        )
    }

    // ── shouldDrop 与 verdict 同源 ────────────────────────────────────
    @Test
    fun `shouldDrop mirrors verdict`() {
        assertFalse(
            StaleStreamPublishPolicy.shouldDrop(
                rowExists = true, rowIsStreaming = true, hasSideChannelEntry = false,
            ),
        )
        assertTrue(
            StaleStreamPublishPolicy.shouldDrop(
                rowExists = true, rowIsStreaming = false, hasSideChannelEntry = false,
            ),
        )
        assertFalse(
            StaleStreamPublishPolicy.shouldDrop(
                rowExists = false, rowIsStreaming = false, hasSideChannelEntry = false,
            ),
        )
    }

    // ── 序列回放：打断前后一整串发布尝试 ──────────────────────────────
    /**
     * Replays the interrupt sequence at the policy level. Before the cancel,
     * the row is streaming and every per-line publish is admitted (this is what
     * renders the live capsules). After `handleUserCancelledCleanup` finalizes
     * the row (isStreaming=false, CANCELLED blocks) and
     * `flushAllStreamingDeltas` drains the side channel, the queued late
     * publishes — one per streamed shell line, plus the engine's
     * already-dispatched Main publishes and the trailing job — must ALL be
     * dropped, or the last one wins and wipes the CANCELLED capsule state.
     */
    @Test
    fun `interrupt sequence drops every late publish after the cleanup`() {
        val liveRowStreaming = true
        val finalizedRowStreaming = false

        // 1) Pre-cancel: 200 streamed lines each publish; row still live.
        val admittedBefore = (1..200).count {
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = liveRowStreaming,
                hasSideChannelEntry = true,
            ) == StaleStreamPublishPolicy.Verdict.PUBLISH
        }
        assertEquals("all live-turn publishes render", 200, admittedBefore)

        // 2) cancelStream: cleanup finalized the row; flush drained the entry.
        //    Every writer that survived streamJob.cancel() lands here.
        val admittedAfter = (1..200).count {
            StaleStreamPublishPolicy.verdict(
                rowExists = true,
                rowIsStreaming = finalizedRowStreaming,
                hasSideChannelEntry = false,
            ) == StaleStreamPublishPolicy.Verdict.PUBLISH
        }
        assertEquals("no late writer may re-mint the overlay entry", 0, admittedAfter)
    }
}
