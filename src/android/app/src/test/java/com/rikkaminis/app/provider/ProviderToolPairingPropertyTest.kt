package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Property-based tests for [sanitizeToolPairing] (2026-10-12).
 *
 * The hand-written samples in [RequestSanitizersTest] pin the documented
 * Pass1/Pass2/Pass3 contract shapes, but the input space of "arbitrary
 * malformed message chains" (repeated roles, colliding/duplicate/empty tool
 * ids, truncated turns, mixed part orderings) cannot be covered by fixed
 * samples. This class seeds a hand-rolled LCG random generator and blasts
 * [sanitizeToolPairing] with adversarial chains, asserting structural
 * invariants INV-A..INV-F on every generated case:
 *
 *  - INV-A  no orphan tool_use survives: every ToolUse in an output assistant
 *           message is answered by a ToolResult with the same id in the
 *           immediately following output message (which must exist and be USER).
 *  - INV-B  no orphan tool_result survives: every ToolResult in an output user
 *           message is answered by a ToolUse with the same id in the
 *           immediately preceding output message (which must exist and be
 *           ASSISTANT).
 *  - INV-C  idempotence: sanitize(sanitize(x)) == sanitize(x).
 *  - INV-D  no reorder / no additions: output has one output message per input
 *           message, in order, same role/content, and each output message's
 *           parts are an order-preserving subsequence of the input parts.
 *  - INV-E  well-formed pass-through: inputs where every tool_use is
 *           immediately answered (and no dangling results) come back unchanged.
 *  - INV-F  totality: never throws, always returns a List (10k messages, 4KB
 *           ids, all-orphan chains included).
 *
 * Fixed regression shapes from the KDoc contract (D-class request-construction
 * audit, 2026-08-14 — not from现场 logs) are pinned separately below.
 *
 * No third-party PBT library: the generator is a hand-rolled LCG with an
 * explicit seed, so every failure is replayable from the printed seed alone.
 */
class ProviderToolPairingPropertyTest {

    // ─── hand-rolled seeded generator (no new dependencies) ───────────────

    /** Deterministic LCG (Numerical Recipes constants). One instance per seed. */
    private class LCGPseudoRandom(seed: Long) {
        private var state: Long = seed xor -7046029254386353131L // 0x9E3779B97F4A7C15 as signed Long
        fun nextLong(): Long {
            state = state * 6364136223846793005L + 1442695040888963407L
            return state
        }
        /** Uniform in [0, bound). */
        fun nextInt(bound: Int): Int {
            require(bound > 0)
            return ((nextLong() ushr 1) % bound).toInt()
        }
        /** True with probability [percentPercent] percent. */
        fun chance(percent: Int): Boolean = nextInt(100) < percent
        fun <T> pick(items: List<T>): T = items[nextInt(items.size)]
    }

    // ─── message/part helpers (mirror RequestSanitizersTest style) ────────

    private fun user(text: String, parts: List<AgentContentPart> = emptyList()) =
        LLMMessage(role = LLMMessage.Role.USER, content = text, contentParts = parts)

    private fun assistant(text: String, parts: List<AgentContentPart> = emptyList()) =
        LLMMessage(role = LLMMessage.Role.ASSISTANT, content = text, contentParts = parts)

    private fun toolUse(id: String, name: String = "shell_execute") =
        AgentContentPart.ToolUse(id = id, name = name, input = JSONObject())

    private fun toolResult(
        id: String,
        name: String = "shell_execute",
        content: String = "ok",
        isError: Boolean = false,
    ) = AgentContentPart.ToolResult(id = id, name = name, content = content, isError = isError)

    // ─── structural rendering (replay snippets + content comparison) ──────
    // Rendered strings deliberately avoid JSONObject.equals quirks: parts are
    // compared field-by-field, so INV-C/INV-E equality is structural.

    private fun renderPart(p: AgentContentPart): String = when (p) {
        is AgentContentPart.Text -> "Text(${p.text})"
        is AgentContentPart.ToolUse -> "ToolUse(id=${p.id},name=${p.name})"
        is AgentContentPart.ToolResult ->
            "ToolResult(id=${p.id},name=${p.name},content=${p.content},isError=${p.isError})"
        else -> p.toString()
    }

    private fun renderMsg(m: LLMMessage): String =
        "${m.role}|content=${m.content}|[${m.contentParts.joinToString(";") { renderPart(it) }}]"

    private fun render(msgs: List<LLMMessage>): String =
        msgs.joinToString("\n") { renderMsg(it) }

    // ─── adversarial generator ─────────────────────────────────────────────
    //
    // Id pool is deliberately TINY (3 short ids + one empty id) so collisions,
    // duplicate ids, "same id used N times" and empty-string ids occur
    // constantly. Roles are a coin flip so consecutive same-role messages and
    // truncated turns (assistant tool_use with no user answer) are common.

    private val idPool = listOf("t1", "t2", "t3", "")
    private val textPool = listOf("hi", "", "do it", "请停止")

    private fun malformedChain(rnd: LCGPseudoRandom): List<LLMMessage> {
        val n = rnd.nextInt(8) + 1 // 1..8 messages
        val msgs = ArrayList<LLMMessage>(n)
        repeat(n) {
            val role = if (rnd.chance(50)) LLMMessage.Role.USER else LLMMessage.Role.ASSISTANT
            val partCount = rnd.nextInt(5) // 0..4 parts, includes empty messages
            val parts = ArrayList<AgentContentPart>(partCount)
            repeat(partCount) {
                when (rnd.nextInt(10)) {
                    in 0..3 -> parts.add(AgentContentPart.Text(rnd.pick(textPool)))
                    in 4..6 -> parts.add(toolUse(id = rnd.pick(idPool), name = rnd.pick(textPool)))
                    else -> parts.add(
                        toolResult(
                            id = rnd.pick(idPool),
                            name = rnd.pick(textPool),
                            content = rnd.pick(textPool),
                            isError = rnd.chance(20),
                        )
                    )
                }
            }
            val text = rnd.pick(textPool)
            msgs.add(if (role == LLMMessage.Role.USER) user(text, parts) else assistant(text, parts))
        }
        return msgs
    }

    /**
     * Well-formed per the KDoc pairing contract: every tool_use is answered by
     * a same-id tool_result in the immediately following USER message, and no
     * user message carries a result that does not match the preceding
     * assistant's (kept) uses.
     */
    private fun wellFormedChain(rnd: LCGPseudoRandom): List<LLMMessage> {
        val msgs = ArrayList<LLMMessage>()
        if (rnd.chance(60)) msgs.add(user(rnd.pick(textPool)))
        while (msgs.size + 2 <= 8) {
            if (rnd.chance(65)) {
                // assistant turn with 1..3 tool calls, then the user answer
                val ids = List(rnd.nextInt(3) + 1) { rnd.pick(idPool) } // dups allowed: set semantics
                msgs.add(assistant(rnd.pick(textPool), ids.map { toolUse(it) }))
                val extraText = if (rnd.chance(30)) listOf(AgentContentPart.Text(rnd.pick(textPool))) else emptyList()
                msgs.add(user("", extraText + ids.map { toolResult(it, isError = rnd.chance(20)) }))
            } else {
                msgs.add(assistant(rnd.pick(textPool)))
                if (rnd.chance(50) && msgs.size + 1 <= 8) msgs.add(user(rnd.pick(textPool)))
            }
        }
        // Trailing assistant must not carry tool_use (nothing follows to answer it).
        if (msgs.isNotEmpty() && msgs.last().role == LLMMessage.Role.ASSISTANT && rnd.chance(50)) {
            msgs.add(user(rnd.pick(textPool)))
        }
        return msgs
    }

    // ─── invariant runner: every failure prints seed + full replay ────────

    private fun assertInvariants(seed: Long, input: List<LLMMessage>, kind: String) {
        val logs = mutableListOf<String>()
        val out = try {
            sanitizeToolPairing(input) { logs.add(it) }
        } catch (e: Throwable) {
            throw AssertionError(
                "INV-F violated ($kind): sanitizeToolPairing threw on seed=$seed\n" +
                    "log=$logs\n=== INPUT ===\n${render(input)}", e
            )
        }
        try {
            // INV-D — no reorder, no additions: 1:1 positional correspondence.
            assertEquals(
                "INV-D ($kind): output message count must equal input (seed=$seed)",
                input.size, out.size
            )
            for (i in input.indices) {
                assertEquals(
                    "INV-D ($kind): role changed at idx=$i (seed=$seed)",
                    input[i].role, out[i].role
                )
                assertEquals(
                    "INV-D ($kind): content changed at idx=$i (seed=$seed)",
                    input[i].content, out[i].content
                )
                // parts: order-preserving subsequence of the original parts
                var p = 0
                for (op in out[i].contentParts) {
                    while (p < input[i].contentParts.size && renderPart(input[i].contentParts[p]) != renderPart(op)) p++
                    assertTrue(
                        "INV-D ($kind): output part at msg=$i not an order-preserving subset " +
                            "(${renderPart(op)}) (seed=$seed)", p < input[i].contentParts.size
                    )
                    p++
                }
            }

            // INV-A — no orphan tool_use survives.
            for (i in out.indices) {
                if (out[i].role != LLMMessage.Role.ASSISTANT) continue
                val uses = out[i].contentParts.filterIsInstance<AgentContentPart.ToolUse>()
                if (uses.isEmpty()) continue
                val next = out.getOrNull(i + 1)
                if (next == null || next.role != LLMMessage.Role.USER) {
                    assertTrue(
                        "INV-A ($kind): assistant tool_use kept with no following USER message " +
                            "at idx=$i (seed=$seed)", uses.isEmpty()
                    )
                } else {
                    val answered = next.contentParts.filterIsInstance<AgentContentPart.ToolResult>().map { it.id }.toSet()
                    for (u in uses) assertTrue(
                        "INV-A ($kind): orphan tool_use id=${u.id} survived at idx=$i (seed=$seed)",
                        answered.contains(u.id)
                    )
                }
            }

            // INV-B — no orphan tool_result survives.
            for (i in out.indices) {
                if (out[i].role != LLMMessage.Role.USER) continue
                val results = out[i].contentParts.filterIsInstance<AgentContentPart.ToolResult>()
                if (results.isEmpty()) continue
                val prev = out.getOrNull(i - 1)
                if (prev == null || prev.role != LLMMessage.Role.ASSISTANT) {
                    assertTrue(
                        "INV-B ($kind): user tool_result kept with no preceding ASSISTANT " +
                            "at idx=$i (seed=$seed)", results.isEmpty()
                    )
                } else {
                    val uses = prev.contentParts.filterIsInstance<AgentContentPart.ToolUse>().map { it.id }.toSet()
                    for (r in results) assertTrue(
                        "INV-B ($kind): orphan tool_result id=${r.id} survived at idx=$i (seed=$seed)",
                        uses.contains(r.id)
                    )
                }
            }

            // INV-C — idempotence.
            val logs2 = mutableListOf<String>()
            val out2 = sanitizeToolPairing(out) { logs2.add(it) }
            assertEquals(
                "INV-C ($kind): sanitize is not idempotent (seed=$seed)\n" +
                    "=== PASS1 OUT ===\n${render(out)}\n=== PASS2 OUT ===\n${render(out2)}",
                render(out), render(out2)
            )
        } catch (e: Throwable) {
            throw AssertionError(
                "log=$logs\n" +
                    "=== INPUT ===\n${render(input)}\n=== OUTPUT ===\n${render(out)}", e
            )
        }
    }

    // ─── property sweeps ───────────────────────────────────────────────────

    @Test
    fun `property - malformed chains preserve pairing invariants across seed sweep`() {
        val seeds = 1..800L
        for (seed in seeds) {
            val rnd = LCGPseudoRandom(seed)
            assertInvariants(seed, malformedChain(rnd), kind = "malformed")
        }
    }

    @Test
    fun `property - idempotence on adversarial chains (explicit double run)`() {
        for (seed in 801L..1200L) {
            val rnd = LCGPseudoRandom(seed)
            val input = malformedChain(rnd)
            val once = sanitizeToolPairing(input)
            val twice = sanitizeToolPairing(once)
            assertEquals("seed=$seed", render(once), render(twice))
        }
    }

    @Test
    fun `property - well-formed chains pass through unchanged`() {
        for (seed in 1..400L) {
            val rnd = LCGPseudoRandom(seed * 7919L)
            val input = wellFormedChain(rnd)
            val out = sanitizeToolPairing(input)
            assertEquals(
                "INV-E violated: well-formed input was modified (seed=${seed * 7919L})\n" +
                    "=== INPUT ===\n${render(input)}\n=== OUTPUT ===\n${render(out)}",
                render(input), render(out)
            )
        }
    }

    @Test
    fun `property - INV-F totality at scale (10k messages, 4KB ids, all orphans)`() {
        // 10k alternating messages, every tool_use orphaned (next user has text only).
        val big = ArrayList<LLMMessage>(10_000)
        val longId = "x".repeat(4096)
        for (i in 0 until 5000) {
            big.add(assistant("a$i", listOf(toolUse(longId + i))))
            big.add(user("no answer here $i"))
        }
        val out = sanitizeToolPairing(big)
        assertEquals(10_000, out.size)
        assertTrue(out[0].contentParts.filterIsInstance<AgentContentPart.ToolUse>().isEmpty())
        // all-orphan results at head, empty-string ids
        val orphans = listOf(
            user("", listOf(toolResult(""), toolResult(""), toolResult(""))),
            user("", listOf(toolResult(longId))),
        )
        sanitizeToolPairing(orphans) // must not throw
        assertTrue(sanitizeToolPairing(orphans)[0].contentParts.isEmpty())
    }

    // ─── fixed regression shapes (from KDoc / 08-14 audit, not现场 logs) ───

    @Test
    fun `regression - compact slice cut between tool_use and its result strips the use`() {
        // KDoc Pass1: "a compact slice cut between a tool_use and its result".
        val msgs = listOf(
            user("hi"),
            assistant("calling", listOf(toolUse("t1"))),
            user("interrupted with plain text"),
        )
        val out = sanitizeToolPairing(msgs)
        assertTrue(out[1].contentParts.filterIsInstance<AgentContentPart.ToolUse>().isEmpty())
    }

    @Test
    fun `regression - unknown-id orphan tool_result is stripped`() {
        // KDoc Pass2: unexpected tool_use_id with no corresponding tool_use block.
        val msgs = listOf(
            assistant("plain text"),
            user("", listOf(toolResult("ghost"))),
        )
        val out = sanitizeToolPairing(msgs)
        assertTrue(out[1].contentParts.filterIsInstance<AgentContentPart.ToolResult>().isEmpty())
    }

    @Test
    fun `regression - stripped-to-empty payload is returned as-is (pass3 NOT in sanitizer)`() {
        // KDoc explicitly keeps pass3 out of the sanitizer; the empty message
        // must NOT be dropped here (callers filter empties themselves).
        val msgs = listOf(
            user("hi"),
            assistant("", listOf(toolUse("t1"))),
            user("please stop"),
        )
        val out = sanitizeToolPairing(msgs)
        assertEquals(3, out.size)
        assertEquals(LLMMessage.Role.ASSISTANT, out[1].role)
        assertTrue(out[1].contentParts.isEmpty())
    }
}
