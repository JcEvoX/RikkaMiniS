package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/dv21-b3] — deep-audit-1041 B-03 (P1).
 *
 * `EnvironmentVariablesScreen` reached its repository from six places, none of
 * them on a background dispatcher. Four are click handlers (copy in the grouped
 * list, copy in the uncategorised list, delete confirm, save/add) and two are
 * composition-phase derivations (`EnvVarListRow.displayValue` and
 * `EnvVarFormSheet.currentSecretValue`). Every one of them is blocking:
 *
 *  * `getValue(key)` → `EncryptedSharedPreferences.getString` — a Keystore-backed
 *    decrypt, and the first call also runs `MasterKey.Builder` +
 *    `EncryptedSharedPreferences.create` (the file holds
 *    `synchronized(lock)`-guarded reads);
 *  * `add/update/delete` → the same Keystore write PLUS `saveMetadata()`, which
 *    rewrites the whole `env-vars.json` (`metadataFile.writeText(...)`).
 *
 * Two shapes are pinned here. Threading binds the DECISION POINT — the
 * dispatcher the call runs on — never one spelling of it: a bare call site is
 * legal only when the callee wraps its own body (the repository-side
 * alternative). And the two composition reads bind the SHAPE that makes them
 * off-Main and leak-free: an effect (`produceState`) whose first frame paints
 * the mask, plus `key(entry.id)` identity so a slot that gets reused by the next
 * entry can never paint the previous entry's secret.
 *
 * Deliberately NOT asserted: `isValidKey` / `isDuplicateKey` are pure
 * (a regex match and an in-memory `_entries.value` scan); they stay on Main and
 * must not be wrapped. Expected values below are literals, never derived from
 * the code under test, so a mutation cannot masquerade as PASS.
 */
class EnvironmentVariablesOffMainTest {

    private val envScreen: String by lazy {
        readRepoFile(
            "app/src/main/java/com/rikkaminis/app/ui/settings/EnvironmentVariablesScreen.kt",
        )
    }

    // ── ① every blocking repository call runs off Main ───────────────────

    @Test
    fun everyBlockingRepositoryCall_runsOffMain() {
        val code = codeOnly(envScreen)
        for (call in listOf(
            "envVarRepository.getValue(",
            "envVarRepository.add(",
            "envVarRepository.update(",
            "envVarRepository.delete(",
        )) {
            assertAllOffMain(code, call, "EnvironmentVariablesScreen")
        }
    }

    @Test
    fun everyBlockingCallSiteIsStillPresent() {
        // Counter-control for the sweep above: a "fix" that deletes the call
        // sites would trivially satisfy it. The screen has exactly six blocking
        // sites — two copy handlers, the delete confirm, the save button, and the
        // two composition-phase reads.
        val code = codeOnly(envScreen)
        assertEquals(
            "expected getValue/add/update/delete call-site counts (4/1/1/1): the two " +
                "copy handlers and the two composition reads all decrypt, so getValue " +
                "carries four sites",
            listOf(4, 1, 1, 1),
            listOf(
                occurrences(code, "envVarRepository.getValue("),
                occurrences(code, "envVarRepository.add("),
                occurrences(code, "envVarRepository.update("),
                occurrences(code, "envVarRepository.delete("),
            ),
        )
        // The two pure checks stay where they are (Main is fine — no I/O).
        assertTrue(
            "isValidKey call site vanished",
            code.contains("envVarRepository.isValidKey(normalizedKey)"),
        )
        assertTrue(
            "isDuplicateKey call site vanished",
            code.contains("envVarRepository.isDuplicateKey(normalizedKey, excludeId = editEntry?.id)"),
        )
    }

    // ── ② the composition-phase reads are effects, not inline reads ──────

    @Test
    fun compositionReads_areProducedByAnEffect() {
        val code = codeOnly(envScreen)
        assertTrue(
            "the row's value read must be produced by a keyed state effect",
            code.contains("val revealedValue by produceState("),
        )
        assertTrue(
            "the form's live secret must be produced by a keyed state effect",
            code.contains("val currentSecretValue by produceState("),
        )
    }

    @Test
    fun compositionReads_areNotInlineDerivations() {
        // The pre-fix shape: `val x = envVarRepository.getValue(...)` evaluated
        // while composing. Neither read may be a plain inline derivation again
        // (a `produceState` that merely *contains* the call is fine, but an
        // assignment at the declaration is not).
        val code = codeOnly(envScreen)
        for (inline in listOf(
            "val revealedValue = if (visible)",
            "val displayValue = if (visible) {",
            "val currentSecretValue = editEntry?.let",
        )) {
            assertFalse(
                "`$inline` is back — the Keystore decrypt runs during composition",
                code.contains(inline),
            )
        }
    }

    @Test
    fun rowsAreIdentityKeyed_soASlotReuseCannotPaintAStaleSecret() {
        // The revealed value now lives in a remembered state: without identity
        // keys, deleting a revealed row hands its slot (and its state) to the
        // next entry, which would paint the deleted entry's secret for a frame.
        val code = codeOnly(envScreen)
        assertEquals(
            "both list loops must wrap their row in key(entry.id) { … }",
            2,
            occurrences(code, "key(entry.id) {"),
        )
    }

    @Test
    fun firstFramePaintsTheMask_notAValue() {
        val code = codeOnly(envScreen)
        assertTrue(
            "the row's loading state must be the mask constant",
            code.contains("initialValue = MASKED_ENV_VAR_VALUE"),
        )
        assertTrue(
            "the mask constant must still be the eight-bullet placeholder",
            code.contains("private const val MASKED_ENV_VAR_VALUE = \"\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\""),
        )
        assertTrue(
            "a hidden row must paint the mask whatever the effect last produced",
            code.contains("val displayValue = if (visible) revealedValue else MASKED_ENV_VAR_VALUE"),
        )
    }

    // ── ③ the click handlers keep the sibling-screen shape ───────────────

    @Test
    fun saveCapturesComposeOwnedValuesOnMain_thenWritesOnIo() {
        val code = codeOnly(envScreen)
        for (literal in listOf(
            "val editingId = editEntry?.id",
            "val saveKey = keyText",
            "val saveValue = valueText",
            "val saveNote = noteText",
            "val saveGroup = groupText",
            "envVarRepository.update(editingId, saveKey, saveValue, saveNote, saveGroup)",
            "envVarRepository.add(saveKey, saveValue, saveNote, saveGroup)",
            "if (success) onDismiss()",
        )) {
            assertTrue("shape literal missing: $literal", code.contains(literal))
        }
    }

    @Test
    fun deleteClosesTheDialogOnMainBeforeTheWriteStarts() {
        val code = codeOnly(envScreen)
        val anchor = code.indexOf("val id = deleteEntryId")
        assertTrue(
            "the delete confirm must capture the target id before clearing the dialog state",
            anchor > 0,
        )
        val tail = code.substring(anchor)
        val close = tail.indexOf("deleteEntryId = null")
        val write = tail.indexOf("envVarRepository.delete(id)")
        assertTrue("delete no longer clears the dialog state", close > 0)
        assertTrue("delete no longer calls the repository through the captured id", write > 0)
        assertTrue(
            "the dialog must be dismissed before the IO write is launched (it must not " +
                "absorb a second tap or flicker while the write is in flight)",
            close < write,
        )
    }

    // ── ④ anchors the other gates and tests pin must NOT move ────────────

    @Test
    fun orderSensitiveLiterals_survive() {
        val code = codeOnly(envScreen)
        for (literal in listOf(
            // deeplink_settings_routes_check criterion 8 / DeepLinkReactiveConsumeTest
            "pendingEnvVarCreate.collectAsState()",
            "LaunchedEffect(pendingCreate)",
            "DeepLinkCoordinator.consumePendingEnvVarCreate()",
            // provider_remember_key_check C16 / DeepLinkReactiveConsumeTest
            "LaunchedEffect(editEntryId, entries)",
            "editTargetVanished(entries, editEntryId)",
            "if (showAddSheet || editEntryId != null)",
            // criterion 10: the four field drafts stay keyed on their source
            "var keyText by remember(editEntry?.id, editEntry?.key, prefillKey)",
            "var valueText by remember(editEntry?.id, currentSecretValue, prefillValue)",
            "var noteText by remember(editEntry?.id, editEntry?.note, prefillNote)",
            "var groupText by remember(editEntry?.id, editEntry?.group)",
        )) {
            assertTrue("shape literal missing: $literal", code.contains(literal))
        }
    }

    // ── ⑤ the helper has teeth (negative controls) ──────────────────────

    @Test
    fun helper_flagsABareCallAndAcceptsAWrappedOne() {
        val bare = "fun a() {\n    envVarRepository.delete(\"x\")\n}\n"
        assertEquals(
            "a bare Main-thread call must be reported",
            listOf(2),
            bareCallLines(bare, "envVarRepository.delete("),
        )
        val wrapped =
            "fun a() {\n    scope.launch {\n        withContext(Dispatchers.IO) {\n" +
                "            envVarRepository.delete(\"x\")\n        }\n    }\n}\n"
        assertTrue(
            "a wrapped call must not be reported",
            bareCallLines(wrapped, "envVarRepository.delete(").isEmpty(),
        )
    }

    @Test
    fun helper_doesNotMistakeProduceStateForABackgroundDispatcher() {
        // The audit's suggested shape ("produceState 包裹") is NOT sufficient on
        // its own: a produceState body runs in the composition's coroutine
        // context (AndroidUiDispatcher / Main), so a blocking call inside it
        // without withContext still blocks the main thread. The criterion is the
        // dispatcher, and the helper must say so.
        val unguarded =
            "fun a() {\n    val v by produceState(\"\", 1) {\n" +
                "        value = envVarRepository.getValue(\"k\") ?: \"\"\n    }\n}\n"
        assertEquals(
            "a produceState body without withContext must still be reported",
            listOf(3),
            bareCallLines(unguarded, "envVarRepository.getValue("),
        )
        val guarded =
            "fun a() {\n    val v by produceState(\"\", 1) {\n" +
                "        value = withContext(Dispatchers.IO) {\n" +
                "            envVarRepository.getValue(\"k\") ?: \"\"\n        }\n    }\n}\n"
        assertTrue(
            "a produceState body guarded by withContext(Dispatchers.IO) must not be reported",
            bareCallLines(guarded, "envVarRepository.getValue(").isEmpty(),
        )
    }

    // ── helpers (independent of the scanner gate's implementation) ──────

    private fun assertAllOffMain(code: String, call: String, label: String) {
        assertTrue("$label: call site not found: $call", code.contains(call))
        val bare = bareCallLines(code, call)
        assertTrue(
            "$label: `$call` runs on Main at line(s) $bare — wrap it in " +
                "withContext(Dispatchers.IO), or move the IO into the repository " +
                "method (the criterion is the dispatcher the call runs on, not the " +
                "spelling)",
            bare.isEmpty(),
        )
    }

    private fun occurrences(code: String, needle: String): Int {
        var n = 0
        var from = 0
        while (true) {
            val i = code.indexOf(needle, from)
            if (i < 0) return n
            n++
            from = i + 1
        }
    }

    /** 1-based line numbers of `call` occurrences NOT inside an IO wrap. */
    private fun bareCallLines(code: String, call: String): List<Int> {
        val out = mutableListOf<Int>()
        var from = 0
        while (true) {
            val idx = code.indexOf(call, from)
            if (idx < 0) break
            if (!isInsideIoWrap(code, idx)) {
                out.add(code.substring(0, idx).count { it == '\n' } + 1)
            }
            from = idx + 1
        }
        return out
    }

    /** True when `offset` sits inside a `withContext(Dispatchers.IO) { … }` or
     *  `launch(Dispatchers.IO) { … }` block. */
    private fun isInsideIoWrap(raw: String, offset: Int): Boolean {
        val code = maskStringsAndComments(raw)
        val stack = ArrayDeque<Boolean>()
        var i = 0
        while (i < offset && i < code.length) {
            when (code[i]) {
                '{' -> {
                    val head = code.substring(0, i).trimEnd().removeSuffix("{").trimEnd()
                    stack.addLast(
                        head.endsWith("withContext(Dispatchers.IO)") ||
                            head.endsWith("launch(Dispatchers.IO)"),
                    )
                }
                '}' -> if (stack.isNotEmpty()) stack.removeLast()
            }
            i++
        }
        return stack.any { it }
    }

    /** Drops whole-line `//` comments so prose cannot satisfy a shape probe. */
    private fun codeOnly(src: String): String = src
        .split("\n")
        .map { if (it.trimStart().startsWith("//")) " ".repeat(it.length) else it }
        .joinToString("\n")

    /** Blanks string bodies and `//` comments (length-preserving) so braces
     *  inside `${...}` templates cannot skew the brace walk. */
    private fun maskStringsAndComments(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '"') {
                if (src.startsWith("\"\"\"", i)) {
                    val j = src.indexOf("\"\"\"", i + 3).let { if (it < 0) src.length else it + 3 }
                    out.append("x".repeat(j - i))
                    i = j
                } else {
                    var j = i + 1
                    while (j < src.length) {
                        if (src[j] == '\\') { j += 2; continue }
                        if (src[j] == '"' || src[j] == '\n') { j++; break }
                        j++
                    }
                    out.append("x".repeat(j - i))
                    i = j
                }
            } else if (src.startsWith("//", i)) {
                val j = src.indexOf('\n', i).let { if (it < 0) src.length else it }
                out.append(" ".repeat(j - i))
                i = j
            } else {
                out.append(c)
                i++
            }
        }
        return out.toString()
    }

    private fun readRepoFile(relative: String): String {
        val cwd = System.getProperty("user.dir") ?: "."
        var dir: java.io.File? = java.io.File(cwd)
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException("$relative not found from user.dir=$cwd")
    }
}
