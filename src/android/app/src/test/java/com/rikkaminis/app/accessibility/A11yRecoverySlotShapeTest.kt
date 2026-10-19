package com.rikkaminis.app.accessibility

import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1030 ISSUE-07 裂缝B] Source-shape probe for the repair-prompt slot
 * in [AccessibilityRecoveryManager].
 *
 * The defect is a *shape* defect: `continuation` is published by the waiting
 * coroutine and taken by `respond` on the UI thread, while the timeout and the
 * cancellation paths tear it down. With no owner identity, a LATE teardown from
 * an expired waiter dismissed the prompt a newer waiter had just published, so
 * that waiter sat parked for its whole timeout with nothing left on screen to
 * answer (the same missing token is what let a stale responder deliver a
 * decision to the wrong waiter). Nothing here is observable from a JVM unit test
 * without a live service, so the four properties that make the hand-off safe are
 * asserted on the production text:
 *
 * 1. the slot is `@Volatile` (written by the waiter, read by the UI thread);
 * 2. a lock arbitrates publish / teardown / respond;
 * 3. `awaitDecision` claims a token BEFORE it publishes, and both of its
 *    teardown paths (timeout, cancellation) go through the token-guarded clear;
 * 4. `respond` takes the slot under the lock and resumes inside `runCatching`,
 *    because an already-cancelled continuation throws through the UI callback.
 *
 * Every arm below asserts its own failure reason, and each mutation is applied to
 * the LIVE source (plus a whole pre-fix fixture), so a probe that has quietly
 * stopped looking fails loudly instead of passing.
 */
class A11yRecoverySlotShapeTest {

    private val recoveryRel =
        "src/android/app/src/main/java/com/rikkaminis/app/accessibility/AccessibilityRecoveryManager.kt"

    private val slotDecl = "var continuation: Continuation<Decision>?"
    private val slotLock = "private val slotLock = Any()"
    private val slotRelease = "_pendingPrompt.value = null"

    /** The pre-fix body, quoted from the audit (kept verbatim on purpose). */
    private val preFix = """
        object AccessibilityRecoveryManager {
            private var continuation: Continuation<Decision>? = null

            private suspend fun awaitDecision(prompt: RepairPrompt): Decision {
                val decision = withTimeoutOrNull(PROMPT_TIMEOUT_MS) {
                    suspendCancellableCoroutine<Decision> { cont ->
                        continuation = cont
                        _pendingPrompt.value = prompt
                        cont.invokeOnCancellation {
                            _pendingPrompt.value = null
                            continuation = null
                        }
                    }
                }
                if (decision == null) {
                    _pendingPrompt.value = null
                    continuation = null
                }
                return decision ?: Decision.CANCEL
            }

            fun respond(decision: Decision) {
                _pendingPrompt.value = null
                val c = continuation
                continuation = null
                c?.resume(decision)
            }
        }
    """.trimIndent()

    // ── probe ────────────────────────────────────────────────────────────────

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }

    /**
     * Comment lines blanked out (offsets preserved) so a mention in prose cannot
     * satisfy a shape probe — the KDoc for this slot discusses the very shapes
     * being asserted.
     */
    private fun codeOnly(body: String): String = body.lines().joinToString("\n") { line ->
        val t = line.trimStart()
        if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) {
            " ".repeat(line.length)
        } else {
            line
        }
    }

    private fun sliceFrom(code: String, anchor: String, nextAnchor: String): String {
        val start = code.indexOf(anchor)
        if (start < 0) return ""
        val end = code.indexOf(nextAnchor, start + anchor.length)
        return if (end < 0) code.substring(start) else code.substring(start, end)
    }

    /** Empty list = the shape holds. Each entry names one broken property. */
    private fun slotViolations(rawBody: String): List<String> {
        val code = codeOnly(rawBody)
        val out = mutableListOf<String>()
        val decl = code.lines().firstOrNull { it.contains(slotDecl) }
        when {
            decl == null -> out += "the prompt slot declaration ($slotDecl) is gone"
            !decl.contains("@Volatile") ->
                out += "the slot lost @Volatile: respond reads it on the UI thread"
        }
        if (!code.contains(slotLock)) {
            out += "no slot lock ($slotLock) arbitrates publish / teardown / respond"
        }

        val await = sliceFrom(code, "private suspend fun awaitDecision(", "private fun clearSlotIfOwner(")
        when {
            await.isBlank() -> out += "awaitDecision is gone"
            else -> {
                val claim = await.contains("++promptToken") || await.contains("promptToken += 1")
                if (!claim) {
                    out += "awaitDecision never claims a prompt token (an expired waiter could " +
                        "clear a newer one's prompt)"
                } else if (!await.contains("synchronized(slotLock) { ++promptToken }")) {
                    out += "awaitDecision claims its token outside the lock"
                }
                if (claim && !await.contains("val accepted = synchronized(slotLock) {")) {
                    out += "awaitDecision publishes the slot without the lock"
                }
                if (!await.contains("invokeOnCancellation")) {
                    out += "awaitDecision dropped invokeOnCancellation (a cancelled caller " +
                        "leaves the dialog on screen)"
                } else if (!await.contains("clearSlotIfOwner(token)")) {
                    out += "awaitDecision's teardown is not token-guarded"
                }
            }
        }

        // Release sites: the criterion is attached to the WRITE itself, wherever
        // it lives — a second release path without its own token comparison is
        // exactly how this family loses the guard.
        val lines = code.lines()
        val releases = lines.withIndex().filter { it.value.contains(slotRelease) }
        if (releases.isEmpty()) {
            out += "the prompt slot has no release site ($slotRelease is gone)"
        } else {
            for ((i, _) in releases) {
                val window = lines.subList(maxOf(0, i - 8), i + 1).joinToString("\n")
                if (!window.contains("promptToken != ")) {
                    out += "the slot release at line ${i + 1} is not ownership-guarded " +
                        "(a late teardown can dismiss a newer waiter's prompt)"
                }
            }
        }
        val respond = sliceFrom(code, "fun respond(", "suspend fun ensureGrantOrPrompt(")
        when {
            respond.isBlank() -> out += "respond is gone"
            else -> {
                if (!respond.contains("runCatching")) {
                    out += "respond resumes without runCatching"
                }
                if (!respond.contains("val taken = synchronized(slotLock) {")) {
                    out += "respond takes the slot outside the lock"
                }
            }
        }
        return out
    }

    /** A mutation arm: the text really changed AND is rejected for its own reason. */
    private fun assertArmRejected(label: String, original: String, mutated: String, needle: String) {
        assertNotEquals("the '$label' mutation must actually change the source", original, mutated)
        val violations = slotViolations(mutated)
        assertTrue("'$label' must be rejected by the probe", violations.isNotEmpty())
        assertTrue(
            "'$label' must be rejected for its own reason (got: $violations)",
            violations.any { it.contains(needle) },
        )
    }

    // ── the live source ──────────────────────────────────────────────────────

    @Test
    fun `the live prompt slot carries an owner token, a lock and a caught resume`() {
        val violations = slotViolations(sourceFile(recoveryRel).readText())
        assertTrue("the live slot shape must hold, got: $violations", violations.isEmpty())
    }

    // ── negative controls ────────────────────────────────────────────────────

    @Test
    fun `the pre-fix slot shape is rejected`() {
        val violations = slotViolations(preFix)
        assertTrue("the pre-fix shape must be rejected", violations.isNotEmpty())
        assertTrue("@Volatile must be one of the reasons: $violations",
            violations.any { it.contains("@Volatile") })
        assertTrue("the missing owner token must be one of the reasons: $violations",
            violations.any { it.contains("claims a prompt token") })
        assertTrue("the unguarded release must be one of the reasons: $violations",
            violations.any { it.contains("not ownership-guarded") })
        assertTrue("the bare resume must be one of the reasons: $violations",
            violations.any { it.contains("runCatching") })
    }

    @Test
    fun `dropping the volatile marker is rejected`() {
        assertArmRejected(
            "volatile",
            sourceFile(recoveryRel).readText(),
            sourceFile(recoveryRel).readText().replace(
                "@Volatile private var continuation",
                "private var continuation",
            ),
            "@Volatile",
        )
    }

    @Test
    fun `making the teardown unconditional again is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "unconditional teardown",
            live,
            live.replace("        if (promptToken != token) return null\n", ""),
            "not ownership-guarded",
        )
    }

    @Test
    fun `resuming without runCatching is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "bare resume",
            live,
            live.replace("runCatching { taken?.resume(decision) }", "taken?.resume(decision)"),
            "runCatching",
        )
    }

    @Test
    fun `never claiming a token is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "no token claim",
            live,
            live.replace("val token = synchronized(slotLock) { ++promptToken }", "val token = 0L"),
            "claims a prompt token",
        )
    }

    @Test
    fun `dropping the cancellation cleanup is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "no cancellation cleanup",
            live,
            live.replace(
                "                cont.invokeOnCancellation { clearSlotIfOwner(token) }\n",
                "",
            ),
            "invokeOnCancellation",
        )
    }

    @Test
    fun `publishing without the lock is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "unlocked publish",
            live,
            live.replace(
                "                val accepted = synchronized(slotLock) {",
                "                val accepted = run {",
            ),
            "without the lock",
        )
    }

    @Test
    fun `responding without the lock is rejected`() {
        val live = sourceFile(recoveryRel).readText()
        assertArmRejected(
            "unlocked respond",
            live,
            live.replace(
                "        val taken = synchronized(slotLock) {",
                "        val taken = run {",
            ),
            "takes the slot outside the lock",
        )
    }
}
