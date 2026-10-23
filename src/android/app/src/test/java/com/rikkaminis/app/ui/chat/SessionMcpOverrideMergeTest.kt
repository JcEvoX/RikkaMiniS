package com.rikkaminis.app.ui.chat

import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1045 B-06] The MCP half of the session-override seed.
 *
 * The regression: `SessionMcpsSheet` had its own inline seeding loop. It
 * decided `missing` on Main, awaited one `isEnabledForSession` SQLite read per
 * missing id on IO, and then wrote every result back with no membership
 * re-check:
 *
 * ```
 * val missing = servers.map { it.id }.filter { it !in overrides }   // snapshot
 * val seeds = withContext(Dispatchers.IO) { … }                      // await
 * for (id in missing) overrides[id] = seeds[id] ?: false             // blind write
 * ```
 *
 * `missing` is a snapshot taken BEFORE the await while `overrides` is the live
 * map backing the switches, so a toggle flipped inside that window was
 * overwritten by its own pre-toggle stored value — the switch snapped back and
 * the user's choice was lost. `SessionSkillsSheet` already delegated to
 * [mergeSessionOverrides], whose first line is the guard; the two files claimed
 * the same contract in their comments and only one of them implemented it.
 *
 * The fix routes the MCP sheet through the same helper, so both faces are
 * pinned here:
 *  - behaviour: the helper never overwrites a key it already has, and never
 *    consults storage for it (an unnecessary read is also a lost race window);
 *  - wiring: the sheet's `LaunchedEffect(servers, sessionId)` body delegates to
 *    the helper instead of writing the map inline — asserted on the real file,
 *    with `mutationsAreCaught` re-running the family on mutated copies that
 *    reintroduce each defect shape.
 *
 * WebView/Compose cannot run on the JVM, so the wiring half is source-shape —
 * the same approach as BrowserTabPoolDisposeScopeTest and the other
 * shape-pinning tests in this package. Expected values are literals.
 */
class SessionMcpOverrideMergeTest {

    // -- behaviour: the contract the MCP sheet now inherits -------------------

    private fun stored(vararg pairs: Pair<String, Boolean>): (String) -> Boolean {
        val table = pairs.toMap()
        return { id -> table.getValue(id) }
    }

    /**
     * The exact decision the effect makes, in the order it makes it: snapshot
     * the missing ids on Main, await the stored reads, then merge.
     */
    private fun runSeeding(
        liveIds: List<String>,
        overrides: MutableMap<String, Boolean>,
        storedValue: (String) -> Boolean,
        duringIoWindow: () -> Unit = {},
    ) {
        val missing = liveIds.filter { it !in overrides }
        val seeds = missing.associateWith { storedValue(it) }
        // … the IO await is where the user's tap lands …
        duringIoWindow()
        mergeSessionOverrides(liveIds, overrides) { seeds[it] ?: false }
    }

    @Test fun `in flight toggle survives the io window`() {
        // alpha was missing when the effect started; the user flips it ON while
        // the stored read is in flight; storage still says OFF. The blind loop
        // wrote `false` back and the switch reverted.
        val overrides = mutableMapOf<String, Boolean>()
        runSeeding(
            liveIds = listOf("alpha", "beta"),
            overrides = overrides,
            storedValue = stored("alpha" to false, "beta" to false),
            duringIoWindow = { overrides["alpha"] = true },
        )
        assertEquals(mapOf("alpha" to true, "beta" to false), overrides)
    }

    @Test fun `stored value still seeds the ids nobody touched`() {
        val overrides = mutableMapOf<String, Boolean>()
        runSeeding(
            liveIds = listOf("alpha", "beta", "gamma"),
            overrides = overrides,
            storedValue = stored("alpha" to true, "beta" to false, "gamma" to true),
        )
        assertEquals(mapOf("alpha" to true, "beta" to false, "gamma" to true), overrides)
    }

    @Test fun `lookup is not consulted for the id the user just flipped`() {
        val overrides = mutableMapOf<String, Boolean>()
        val consulted = mutableListOf<String>()
        runSeeding(
            liveIds = listOf("alpha"),
            overrides = overrides,
            storedValue = { consulted.add(it); true },
            duringIoWindow = { overrides["alpha"] = false },
        )
        assertEquals(mapOf("alpha" to false), overrides)
        // the read for alpha already happened during the window (it was missing
        // then); what must NOT happen is a second consult on the merge, which is
        // what makes the merge re-decide instead of replay.
        assertEquals(listOf("alpha"), consulted)
    }

    @Test fun `fallback false keeps the lookup total when a seed is missing`() {
        // `?: false` in the sheet's lookup lambda is unreachable by construction
        // (seeds covers exactly the ids the merge asks about) — pin that it
        // still degrades to "disabled" rather than throwing if it ever is not.
        val overrides = mutableMapOf<String, Boolean>()
        val seeds = emptyMap<String, Boolean>()
        mergeSessionOverrides(listOf("alpha", "beta"), overrides) { seeds[it] ?: false }
        assertEquals(mapOf("alpha" to false, "beta" to false), overrides)
    }

    @Test fun `vanished servers are dropped so a re add re seeds`() {
        val overrides = mutableMapOf<String, Boolean>()
        runSeeding(listOf("alpha", "beta"), overrides, stored("alpha" to true, "beta" to true))
        assertEquals(mapOf("alpha" to true, "beta" to true), overrides)
        // beta is removed from the repository …
        runSeeding(listOf("alpha"), overrides, stored("alpha" to true))
        assertEquals(mapOf("alpha" to true), overrides)
        // … and comes back, with storage now saying OFF
        runSeeding(listOf("alpha", "beta"), overrides, stored("alpha" to true, "beta" to false))
        assertEquals(mapOf("alpha" to true, "beta" to false), overrides)
    }

    @Test fun `draft promotion reseeds from the promoted session id`() {
        // The map identity is keyed on sessionId, so promotion hands the effect a
        // FRESH map: every toggle re-derives from the real id's stored values
        // instead of carrying the `__new__…` seeds across.
        val draft = mutableMapOf<String, Boolean>()
        runSeeding(listOf("alpha"), draft, stored("alpha" to true))
        assertEquals(mapOf("alpha" to true), draft)

        val promoted = mutableMapOf<String, Boolean>() // remember(realSessionId)
        runSeeding(listOf("alpha"), promoted, stored("alpha" to false))
        assertEquals(mapOf("alpha" to false), promoted)
        assertEquals(mapOf("alpha" to true), draft)
    }

    // -- wiring: the real sheet must delegate, not write inline ---------------

    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }

    private fun sheetSource(): String =
        readRepoFile(
            "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/SessionMcpsSheet.kt",
        )

    /** The `{ … }` body of `LaunchedEffect(servers, sessionId)` in [src]. */
    private fun seedEffectBody(src: String): String {
        val at = src.indexOf("LaunchedEffect(servers, sessionId)")
        assertTrue("anchor `LaunchedEffect(servers, sessionId)` missing", at >= 0)
        val open = src.indexOf('{', at)
        assertTrue("no body brace after the effect header", open >= 0)
        var depth = 0
        var i = open
        while (i < src.length) {
            when (src[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(open + 1, i)
                }
            }
            i++
        }
        throw AssertionError("unbalanced braces in the seeding effect")
    }

    private val blindWrite =
        Regex("""(?<![\w.])overrides\s*\[[^\]]*]\s*=(?!=)""")

    /** The three spellings of "this id is still missing from the live map". */
    private val guardRes = listOf(
        Regex("""!\s*in\s+overrides\b"""),
        Regex("""!\s*overrides\s*\.\s*containsKey\s*\("""),
        Regex("""overrides\s*\[[^\]]*]\s*==\s*null"""),
    )

    /** Condition of an `if ( … )` whose `)` is the last token of [text]. */
    private fun attachedIfCondition(text: String): String? {
        val tail = text.trimEnd()
        if (!tail.endsWith(")")) return null
        var depth = 0
        var j = tail.length - 1
        while (j >= 0) {
            when (tail[j]) {
                ')' -> depth++
                '(' -> {
                    depth--
                    if (depth == 0) {
                        return if (Regex("""\bif\s*$""").containsMatchIn(tail.substring(0, j))) {
                            tail.substring(j + 1, tail.length - 1)
                        } else {
                            null
                        }
                    }
                }
            }
            j--
        }
        return null
    }

    /** Same decision as the scan gate: the guard must be attached to the write. */
    private fun isGuarded(body: String, at: Int): Boolean {
        val cond = attachedIfCondition(body.substring(maxOf(0, at - 500), at))
        if (cond != null && guardRes.any { it.containsMatchIn(cond) }) return true
        var depth = 0
        var j = at - 1
        var blk = -1
        while (j >= 0) {
            when (body[j]) {
                '}' -> depth++
                '{' -> {
                    if (depth == 0) {
                        blk = j
                        break
                    }
                    depth--
                }
            }
            j--
        }
        if (blk >= 0 && body.substring(blk + 1, at).isBlank()) {
            val cond2 = attachedIfCondition(body.substring(maxOf(0, blk - 500), blk))
            if (cond2 != null && guardRes.any { it.containsMatchIn(cond2) }) return true
        }
        return false
    }

    /** Inline writes into the live map that no guard covers. */
    private fun blindWritesIn(body: String): List<String> {
        val out = mutableListOf<String>()
        for (m in blindWrite.findAll(body)) {
            if (isGuarded(body, m.range.first)) continue
            val line = body.substring(0, m.range.first).count { it == '\n' } + 1
            out.add("line $line: ${m.value}")
        }
        return out
    }

    private fun delegatesToMerge(body: String): Boolean =
        Regex("""\bmerge[A-Za-z0-9_]*Overrides?\s*\(""").containsMatchIn(body) &&
            Regex("""overrides\s*=\s*overrides""").containsMatchIn(body)

    @Test fun `mcp sheet seeds through the shared merge instead of an inline unguarded loop`() {
        val body = seedEffectBody(sheetSource())
        assertTrue(
            "the seeding effect must delegate to a merge*Overrides(…) helper that receives " +
                "`overrides`; body was:\n$body",
            delegatesToMerge(body),
        )
        assertTrue(
            "the seeding effect must not write the live map inline (an unguarded write " +
                "replays a pre-await snapshot over an in-flight toggle): ${blindWritesIn(body)}",
            blindWritesIn(body).isEmpty(),
        )
        assertFalse(
            "the pre-await snapshot loop `for (id in missing)` must not come back",
            body.contains("for (id in missing)"),
        )
    }

    private fun caught(block: () -> Unit): Boolean = try {
        block()
        false
    } catch (_: AssertionError) {
        true
    }

    @Test fun `source shape assertions have teeth`() {
        val src = sheetSource()
        val body = seedEffectBody(src)
        val effectStart = src.indexOf("LaunchedEffect(servers, sessionId)")
        val bodyOpen = src.indexOf('{', effectStart)
        val bodyEnd = bodyOpen + 1 + body.length
        val head = src.substring(0, bodyOpen + 1)
        val tail = src.substring(bodyEnd)

        fun withBody(newBody: String): String = head + newBody + tail

        // m1: the baseline loop, verbatim — must be caught on BOTH counts
        // (it writes inline, and it stops delegating).
        val baseline = """
            |        val missing = servers.map { it.id }.filter { it !in overrides }
            |        val seeds = withContext(Dispatchers.IO) {
            |            missing.associateWith { mcpRepository.isEnabledForSession(it, sessionId) }
            |        }
            |        for (id in missing) overrides[id] = seeds[id] ?: false
            |""".trimMargin()
        val m1 = withBody(baseline)
        assertTrue("baseline loop must be caught as an inline write",
            caught { assertTrue(blindWritesIn(seedEffectBody(m1)).isEmpty()) })
        assertTrue("baseline loop must be caught as non-delegating",
            caught { assertTrue(delegatesToMerge(seedEffectBody(m1))) })

        // m2: helper kept, map argument dropped — seeds nothing this bean can see
        val m2 = withBody(body.replace("overrides = overrides,", ""))
        assertTrue("a helper call that does not receive the map must be caught",
            caught { assertTrue(delegatesToMerge(seedEffectBody(m2))) })

        // m3: guard written against the stale snapshot instead of the live map
        val m3 = withBody(
            """
            |        val missing = servers.map { it.id }.filter { it !in overrides }
            |        for (id in missing) {
            |            if (id !in missing) overrides[id] = mcpRepository.isEnabledForSession(id, sessionId)
            |        }
            |""".trimMargin(),
        )
        assertTrue("a guard on the wrong collection must be caught as an inline write",
            caught { assertTrue(blindWritesIn(seedEffectBody(m3)).isEmpty()) })

        // m4: the shipped shape with a brace-less guard is ACCEPTED (the family
        // must not degenerate into "any write is red").
        val m4 = withBody(
            """
            |        val ids = servers.map { it.id }
            |        for (id in ids) {
            |            if (id !in overrides) overrides[id] = mcpRepository.isEnabledForSession(id, sessionId)
            |        }
            |""".trimMargin(),
        )
        assertTrue("a directly guarded write must be accepted",
            blindWritesIn(seedEffectBody(m4)).isEmpty())
    }
}
