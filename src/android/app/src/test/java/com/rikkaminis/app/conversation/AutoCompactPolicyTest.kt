package com.rikkaminis.app.conversation

import com.rikkaminis.app.data.ContextPolicy
import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [T5-auto-compact] JVM tests for [ContextCompactor] — the automatic-compaction
 * decision engine (OmniBot AgentConversationContextCompactor parity).
 *
 * The decision must be conservative by construction:
 *  - only fires when ContextPolicy says NEEDS_COMPACT (at/over the compact line,
 *    still below the hard ceiling);
 *  - never re-fires too soon (RECENT_AUTO_COMPACT debounce);
 *  - never fires when there's nothing meaningful to compact (TAIL_TOO_SMALL);
 *  - never stacks on top of an in-flight compact (COMPACT_IN_FLIGHT);
 *  - never fires at/over the hard ceiling (EXHAUSTED — manual /compact or a new
 *    chat is the user's move there).
 */
class AutoCompactPolicyTest {

    private val window = 128_000
    private val policy = ContextPolicy.forContextWindow(window) // compact line = 108K

    private fun decide(
        tokens: Int,
        tail: Long = 40_000,
        isCompacting: Boolean = false,
        lastAuto: Long = Long.MIN_VALUE,
        now: Long = 1_000_000L,
        escalated: Boolean = false,
    ) = ContextCompactor.decide(
        estimatedTokens = tokens,
        contextWindow = window,
        policy = policy,
        tailTokens = tail,
        isCompacting = isCompacting,
        lastAutoCompactAtMs = lastAuto,
        nowMs = now,
        escalatedFromOffload = escalated,
    )

    // ── trigger conditions ────────────────────────────────────────────────

    @Test
    fun `OK below the compact line`() {
        assertEquals(ContextCompactor.Decision.OK, decide(tokens = 100_000))
    }

    @Test
    fun `AUTO_COMPACT at the compact line with healthy tail`() {
        assertEquals(ContextCompactor.Decision.AUTO_COMPACT, decide(tokens = 120_000))
    }

    @Test
    fun `AUTO_COMPACT fires for the 64K tier too`() {
        val w = 77_000
        val p = ContextPolicy.forContextWindow(w) // compact line = 67K
        val d = ContextCompactor.decide(
            estimatedTokens = 70_000, contextWindow = w, policy = p,
            tailTokens = 20_000, isCompacting = false,
        )
        assertEquals(ContextCompactor.Decision.AUTO_COMPACT, d)
    }

    // ── debounce & yield guards ───────────────────────────────────────────

    @Test
    fun `RECENT_AUTO_COMPACT when less than the min interval has elapsed`() {
        val lastAuto = 500_000L
        val now = lastAuto + ContextCompactor.DEFAULT_AUTO_COMPACT_MIN_INTERVAL_MS - 1
        assertEquals(ContextCompactor.Decision.RECENT_AUTO_COMPACT, decide(tokens = 120_000, lastAuto = lastAuto, now = now))
    }

    @Test
    fun `AUTO_COMPACT again once the min interval has elapsed`() {
        val lastAuto = 500_000L
        val now = lastAuto + ContextCompactor.DEFAULT_AUTO_COMPACT_MIN_INTERVAL_MS
        assertEquals(ContextCompactor.Decision.AUTO_COMPACT, decide(tokens = 120_000, lastAuto = lastAuto, now = now))
    }

    @Test
    fun `TAIL_TOO_SMALL when the tail is below the minimum`() {
        // Just auto-compacted: tail is tiny → don't re-compact.
        assertEquals(ContextCompactor.Decision.TAIL_TOO_SMALL, decide(tokens = 120_000, tail = 1_000))
    }

    @Test
    fun `COMPACT_IN_FLIGHT when a compact is already running`() {
        assertEquals(ContextCompactor.Decision.COMPACT_IN_FLIGHT, decide(tokens = 120_000, isCompacting = true))
    }

    @Test
    fun `EXHAUSTED at the hard ceiling - never auto compact past the window`() {
        assertEquals(ContextCompactor.Decision.EXHAUSTED, decide(tokens = window))
    }

    // ── [T-ctx-offload-escalation] escalated compacts ─────────────────────

    @Test
    fun `escalation compacts below the compact line when offload under-delivered`() {
        // Long-session dead band: every large tool result already carries a stub,
        // so the offload pass frees (almost) nothing while the context keeps
        // climbing toward the compact line. The caller that observed the
        // shortfall may escalate one turn early instead of idling in the band.
        assertEquals(ContextCompactor.Decision.OK, decide(tokens = 100_000))
        assertEquals(
            ContextCompactor.Decision.AUTO_COMPACT,
            decide(tokens = 100_000, escalated = true),
        )
    }

    @Test
    fun `escalation still respects the tail gate`() {
        assertEquals(
            ContextCompactor.Decision.TAIL_TOO_SMALL,
            decide(tokens = 100_000, tail = 1_000, escalated = true),
        )
    }

    @Test
    fun `escalation still respects the debounce and the hard ceiling`() {
        assertEquals(
            ContextCompactor.Decision.RECENT_AUTO_COMPACT,
            decide(tokens = 100_000, lastAuto = 999_000L, now = 1_000_000L, escalated = true),
        )
        assertEquals(
            ContextCompactor.Decision.EXHAUSTED,
            decide(tokens = window, escalated = true),
        )
    }

    @Test
    fun `OK when no token estimate or window is unknown`() {
        assertEquals(ContextCompactor.Decision.OK, decide(tokens = 0))
        val d = ContextCompactor.decide(
            estimatedTokens = 50_000, contextWindow = 0, policy = policy,
            tailTokens = 40_000, isCompacting = false,
        )
        assertEquals(ContextCompactor.Decision.OK, d)
    }

    // ── token estimation ──────────────────────────────────────────────────

    @Test
    fun `estimateTokens ascii chars divided by 4, CJK one token per char`() {
        assertEquals(0L, ContextCompactor.estimateTokens("abc")) // 3/4 = 0
        assertEquals(1L, ContextCompactor.estimateTokens("abcd"))
        assertEquals(10L, ContextCompactor.estimateTokens("a".repeat(40)))
        // [fix/cjk-token-estimate] CJK ≈ 1 字 1 token（旧口径 3 字 = 0 token，低估 3-4 倍）
        assertEquals(3L, ContextCompactor.estimateTokens("中文了"))
        assertEquals(3L, ContextCompactor.estimateTokens("abcd中文")) // 1 + 2
        assertEquals(3L, ContextCompactor.estimateTokens("テスト")) // 假名同 CJK 区
        assertEquals(2L, ContextCompactor.estimateTokens("，。")) // 全角标点
    }

    @Test
    fun `estimateMessageTokens counts content plus content parts`() {
        val msg = LLMMessage(
            role = LLMMessage.Role.USER,
            content = "a".repeat(40), // 10 tokens — NOT sent on the wire (parts non-empty)
            contentParts = listOf(
                AgentContentPart.Text("b".repeat(80)),        // 20 tokens
                AgentContentPart.ToolResult("t1", "tool", "c".repeat(160)), // content 40 tokens
                AgentContentPart.ToolUse("t2", "tool", JSONObject()), // {} → 0 tokens
            ),
        )
        // [fix/audit2-estimate-1015] wire 语义：contentParts 非空时只发 parts，
        // `content` 字段被忽略（OpenAIProvider structured-parts 分支）—— 所以是
        // 20 (Text) + 40 (ToolResult.content) + 0 (empty ToolUse input) = 60，
        // 不是旧口径的 10 + 20 + 40 + 0 = 70。
        assertEquals(60, ContextCompactor.estimateMessageTokens(msg).toInt())
    }

    @Test
    fun `estimateMessageTokens counts content when there are no parts`() {
        // legacy 分支：parts 为空才发 content（此处 content 必须计入，不得为 0）
        val msg = LLMMessage(LLMMessage.Role.USER, "a".repeat(40))
        assertEquals(10L, ContextCompactor.estimateMessageTokens(msg))
    }

    @Test
    fun `estimateMessageTokens does not double count content mirrored into a Text part`() {
        // [fix/audit2-estimate-1015] 回归：主发送路径与 assistant 文本轮都是
        // "content = <同一段文本> + contentParts = [Text(<同一段文本>), …]" 的形状
        // （ChatViewModel 用户消息 / AgentLoopEngine assistant turn）。旧实现把这段
        // 文本算两遍；wire 只发 parts，故正确值是 1× 文本。
        val text = "中文".repeat(16) // 32 个 CJK 字符 → 32 tokens（字面量，不从被测算常量推导）
        val doubleFilled = LLMMessage(
            role = LLMMessage.Role.USER,
            content = text,
            contentParts = listOf(AgentContentPart.Text(text)),
        )
        val textTokens = ContextCompactor.estimateTokens(text)
        assertEquals(32L, textTokens) // 字面量：32 个 CJK 字符
        assertEquals(textTokens, ContextCompactor.estimateMessageTokens(doubleFilled))
        assertTrue(
            "双计会让估算翻倍：${ContextCompactor.estimateMessageTokens(doubleFilled)}",
            ContextCompactor.estimateMessageTokens(doubleFilled) < 2 * textTokens,
        )
    }

    @Test
    fun `estimateMessageTokens has a single implementation shared with ContextEstimator`() {
        // 两个入口名背后只能有一份公式（委托，不是两份"差不多"的实现）
        val samples = listOf(
            LLMMessage(LLMMessage.Role.USER, "a".repeat(40)),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "中文".repeat(30),
                contentParts = listOf(AgentContentPart.Text("中文".repeat(30))),
            ),
            LLMMessage(
                role = LLMMessage.Role.ASSISTANT,
                content = "done",
                contentParts = listOf(
                    AgentContentPart.Text("done"),
                    AgentContentPart.ToolUse("t1", "shell_execute", JSONObject("{\"command\":\"ls\"}")),
                ),
                reasoningContent = "先列目录。".repeat(5),
            ),
        )
        for (m in samples) {
            assertEquals(
                ContextEstimator.estimateMessageTokensFull(m),
                ContextCompactor.estimateMessageTokens(m),
            )
        }
    }

    @Test
    fun `estimateTailTokens without anchor covers the whole history`() {
        val msgs = listOf(
            LLMMessage(LLMMessage.Role.USER, "a".repeat(40)),
            LLMMessage(LLMMessage.Role.ASSISTANT, "b".repeat(80)),
        )
        assertEquals(10L + 20L, ContextCompactor.estimateTailTokens(msgs, null))
    }

    @Test
    fun `estimateTailTokens counts only messages after the anchor`() {
        val anchorId = "db-1"
        val msgs = listOf(
            LLMMessage(LLMMessage.Role.USER, "a".repeat(40), dbMessageId = "db-0"), // 10
            LLMMessage(LLMMessage.Role.USER, "b".repeat(40), dbMessageId = anchorId),   // 10 anchor
            LLMMessage(LLMMessage.Role.ASSISTANT, "c".repeat(80), dbMessageId = "db-2"), // 20 tail
        )
        assertEquals(20L, ContextCompactor.estimateTailTokens(msgs, anchorId))
    }

    @Test
    fun `estimateTailTokens falls back to full history when anchor is missing`() {
        val msgs = listOf(
            LLMMessage(LLMMessage.Role.USER, "a".repeat(40), dbMessageId = "db-0"),
            LLMMessage(LLMMessage.Role.ASSISTANT, "b".repeat(80), dbMessageId = "db-2"),
        )
        // anchor id not present in history → full history (mirrors effectiveAgentHistory fallback)
        assertEquals(30L, ContextCompactor.estimateTailTokens(msgs, "ghost-anchor"))
    }

    // ── MUST PRESERVE wording (T5 acceptance 4) ───────────────────────────

    @Test
    fun `compact prompt pins verbatim preservation of paths URLs and UUIDs`() {
        val prompt = ContextCompactor.COMPACT_SUMMARY_SYSTEM_PROMPT.lowercase()
        assertTrue("must contain MUST PRESERVE section", prompt.contains("must preserve"))
        assertTrue("must demand verbatim copy of paths/URLs/UUIDs", prompt.contains("copy verbatim"))
        assertTrue("must cover file paths", prompt.contains("file paths"))
        assertTrue("must cover URLs", prompt.contains("urls"))
        assertTrue("must cover UUIDs", prompt.contains("uuids"))
        assertTrue("must forbid altering code snippets", prompt.contains("do not translate or alter code"))
    }

    // ── SUPERSEDED VALUES clause (task-brief-1005-D §2.B) ─────────────────

    @Test
    fun `compact prompt carries the SUPERSEDED VALUES collapse clause`() {
        // The horizon counter-measure: superseded intermediate values MAY
        // collapse into their latest form, so high-identifier-density
        // conversations stop growing the summary monotonically
        // (compact-exp-1004 arm E). Lower-cased pin on the section header —
        // any rewrite that drops the clause fails here.
        val prompt = ContextCompactor.COMPACT_SUMMARY_SYSTEM_PROMPT.lowercase()
        assertTrue("must contain the SUPERSEDED VALUES clause", prompt.contains("superseded values"))
        assertTrue(
            "clause must live in the MUST PRESERVE prompt (single source of truth)",
            prompt.contains("must preserve") && prompt.indexOf("must preserve") < prompt.indexOf("superseded values"),
        )
    }

    @Test
    fun `SUPERSEDED clause keeps the verbatim final-value seatbelt`() {
        // The safety belt that makes collapsing safe: the FINAL value of
        // every identifier must still appear verbatim exactly once. Pinned
        // as a co-occurrence so a later edit that keeps the clause header
        // but deletes the belt still fails (task-brief-1005-D §3 B-2).
        val prompt = ContextCompactor.COMPACT_SUMMARY_SYSTEM_PROMPT.lowercase()
        val atFinal = prompt.indexOf("final value")
        assertTrue("clause must state the FINAL value rule", atFinal >= 0)
        // Search from atFinal: the MUST PRESERVE bullets already contain an
        // earlier "copy verbatim", which is not the clause's own seatbelt.
        val atVerbatim = prompt.indexOf("verbatim", atFinal)
        assertTrue("clause must keep the verbatim seatbelt", atVerbatim >= 0)
        // Co-occurrence within the clause paragraph, not just the file.
        assertTrue(
            "verbatim seatbelt must sit with the final-value rule",
            (atVerbatim - atFinal).let { it >= 0 && it < 400 },
        )
    }
}