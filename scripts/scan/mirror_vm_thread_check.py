#!/usr/bin/env python3
"""da1022-F + dv25-B1 gate: MirrorSpeedTestViewModel cross-thread snapshot
writes, main-thread IO, speed-test auto-select wiring, and first-boot
auto-detection ownership.

Usage (repo root, positional arg supported):
    python3 scripts/scan/mirror_vm_thread_check.py [ROOT]

PASS  = exit 0, prints MIRROR_VM_THREAD_CHECK=PASS
        (--selftest prints MIRROR_VM_THREAD_SELFTEST=PASS)
FAIL  = exit 1, prints violating arm descriptions

Judged invariants (deep-audit-1022 ISSUE-06 + deep-audit-1045 B-01; all anchors
are CODE anchors on MirrorSettingsScreen.kt — comment lines are stripped first,
so prose quoting an old expression cannot satisfy or trip a criterion):
  1. ensureLoaded: the SnapshotStateMap writes (selectedMirrorId/useCustomMirror)
     must sit inside a `Snapshot.withMutableSnapshot { }` transaction — the
     function is entered from background threads (boot path) and the batch
     must land atomically.
  2. autoDetectOnceIfNeeded: `applyMirror(` must NOT appear inside any
     `withContext(Dispatchers.Main)` block (it is disk IO), and must be
     called in the function body.
  3. speed-test auto-select: the shared test body must write
     `useCustomMirror[cat] = true` and `persist(context, cat)`, and
     `applyMirror(` must be called in the body but NOT inside a Main block
     (applied categories are collected and applied off the main thread).
     The body lives in `runAllTests` and/or its private shared body
     (`startAllTests` after dv25-B1), so both are scanned and the public entry
     point must reach the shared body.
  4. retryApkWorldWithMirrorFallback: the two ALPINE snapshot-state writes
     must sit inside a `Snapshot.withMutableSnapshot { }` transaction.
  5. [dv25-B1] the auto-detect settlement preserves a user selection: the loop
     that writes `selectedMirrorId[...] =` must reject categories that already
     have a selection — inline, or through a local decision function on the
     settlement path (derived from the call graph, never from a function name).
  6. [dv25-B1] the auto-detect coroutine owns a cancellable slot: its launch is
     assigned to a `@Volatile` field, and both user entry points
     (runAllTests / runTest) cancel that field. A bare `scope.launch` — the
     shipped defect — fails here.
  7. [dv25-B1] the settlement is ownership-checked before it writes: the Main
     block holding the settlement must test the owning slot (or liveness).
"""
import re
import sys
import tempfile
from pathlib import Path

REL_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorSettingsScreen.kt"

VIOLATIONS = []

# Texts that mark "this decision reads a prior user selection". A *write*
# (`selectedMirrorId[cat] = …`) must not count — that is the settlement
# itself, and counting it made every unguarded loop look guarded.
PRIOR_SELECTION_RE = re.compile(
    r"\balreadySelected\b"
    r"|\bhasPriorSelection\s*\("
    r"|selectedMirrorId\[\w+\]\s*!=\s*null"
    r"|selectedMirrorId\[\w+\]\s*==\s*null"
    r"|selectedMirrorId\.keys"
)
# Texts that mark "and it rejects those entries".
REJECTION_RE = re.compile(r"\bcontinue\b|\breturn@\w+|\breturn\b")


def code_only(src: str) -> str:
    """Strip comment lines (// and /* */ and KDoc) so a gate key living only
    in a comment cannot fake a pass (COMMON §3)."""
    out = []
    in_block = False
    for line in src.split("\n"):
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("*") or s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def block_after(src: str, opener: str, start: int = 0):
    """Return the balanced-brace block that follows the first occurrence of
    `opener` at/after `start`, or None. Kotlin string templates keep braces
    balanced, so plain counting is safe here."""
    i = src.find(opener, start)
    if i < 0:
        return None
    b = src.find("{", i)
    if b < 0:
        return None
    depth = 0
    for j in range(b, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return (src[b:j + 1], j + 1)
    return None


def fn_body(src: str, sig: str):
    """Return the body block of the function whose signature contains `sig`."""
    i = src.find(sig)
    if i < 0:
        return None
    r = block_after(src, sig, i)
    return r[0] if r else None


def match_paren(text: str, open_idx: int):
    """Index of the ')' matching text[open_idx] == '(' (paren-aware, so a loop
    iterable containing a call's own parens is parsed correctly)."""
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def for_loops(text: str):
    """[(header, body_block)] for every `for (...)` in `text`; header is the
    text inside the parentheses (the iterable expression)."""
    out = []
    i = 0
    while True:
        i = text.find("for (", i)
        if i < 0:
            return out
        close = match_paren(text, text.find("(", i))
        if close < 0:
            return out
        brace = text.find("{", close)
        r = block_after(text, "{", brace) if brace >= 0 else None
        if r is None:
            return out
        out.append((text[i + 5:close], r[0]))
        i = close + 1


def main_blocks(body: str):
    """All `withContext(Dispatchers.Main) { ... }` blocks inside `body`."""
    blocks = []
    i = 0
    while True:
        r = block_after(body, "withContext(Dispatchers.Main)", i)
        if r is None:
            return blocks
        blocks.append(r[0])
        i = r[1]


def fn_slice(src: str, name: str):
    """Text of `fun <name>(...)` — the block body through its closing brace, or
    the expression body up to the next member declaration. Used to walk a
    settlement decision without depending on how it is written."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return None
    rest = src[m.start():]
    brace = rest.find("{")
    eq = rest.find("=")
    paren_end = match_paren(rest, rest.find("("))
    # expression body: `= ...` before any brace
    if eq >= 0 and paren_end >= 0 and eq > paren_end and (brace < 0 or eq < brace):
        nxt = re.search(
            r"\n\n(?=\s*(?:@\w|private |internal |public |override |suspend |fun |val |var |object ))", rest
        )
        return rest[: nxt.start()] if nxt else rest
    if brace < 0:
        return rest
    r = block_after(rest, "{", brace)
    return rest[: brace + len(r[0])] if r else rest


def local_calls(text: str):
    """Function-call names in `text` (candidates for a decision hop)."""
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))


def launch_slot(body: str):
    """The field a launch's Job is published to, plus the local it goes through.

    Two spellings are accepted, both used in this file:
      - `slot = <scope>.launch { ... }`                  -> (slot, None)
      - `val job = <scope>.launch { ... }; slot = job`     -> (slot, "job")
    Returns (None, None) when the coroutine is never published to a field —
    that is the shipped defect ("bare scope.launch", cancelling the inner run
    leaves the settlement alive). A local `val` alone is *not* an ownership
    slot: it cannot be cancelled from another entry point.
    """
    for m in re.finditer(r"([A-Za-z_]\w*)\s*=\s*[\w.]*launch\b", body):
        line_start = body.rfind("\n", 0, m.start()) + 1
        prefix = body[line_start:m.start()]
        lhs = m.group(1)
        if re.search(r"\b(?:val|var)\s+$", prefix):
            pub = re.search(
                r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*" + re.escape(lhs) + r"\b", body[m.end():]
            )
            if pub:
                return pub.group(1), lhs
            continue
        return lhs, None
    return None, None


def has_prior_selection_marker(text: str) -> bool:
    return PRIOR_SELECTION_RE.search(text) is not None


def has_rejection(text: str) -> bool:
    return REJECTION_RE.search(text) is not None


def strip_signature(slice_text: str) -> str:
    """Drop the `fun name(...)` head (and its parameter list) from a function
    slice. Declaring a parameter called `alreadySelected` is not evidence that
    the decision *reads* a prior selection — only its body is."""
    p = slice_text.find("(")
    if p < 0:
        return slice_text
    close = match_paren(slice_text, p)
    return slice_text[close + 1:] if close >= 0 else slice_text


def settlement_guard_ok(src: str, body: str) -> str:
    """Return '' when the auto-detect settlement rejects already-selected
    categories, else a reason string. Accepts an inline guard or a decision
    function reachable from the loop's iterable (two hops max). Only *reads* of
    a prior selection count — the settlement's own writes must not satisfy it.
    """
    loops = [(h, b) for (h, b) in for_loops(body) if re.search(r"selectedMirrorId\[\w+\]\s*=", b)]
    if not loops:
        return "no settlement loop writes selectedMirrorId[...]"
    reasons = []
    for header, block in loops:
        # inline guard: the loop body itself consults the selection and skips
        if has_prior_selection_marker(block) and has_rejection(block):
            return ""
        hop1 = {}
        for n in local_calls(header):
            sl = fn_slice(src, n)
            if sl:
                hop1[n] = strip_signature(sl)
        # single hop: the decision itself reads and rejects
        if any(has_prior_selection_marker(t) and has_rejection(t) for t in hop1.values()):
            return ""
        # delegation: hop1 reads the selection, hop2 rejects against it
        for name, text in hop1.items():
            if not has_prior_selection_marker(text):
                continue
            for n2 in local_calls(text) - {name}:
                sl2 = fn_slice(src, n2)
                if not sl2:
                    continue
                b2 = strip_signature(sl2)
                if has_rejection(b2) and has_prior_selection_marker(b2):
                    return ""
        reasons.append(
            "settlement loop over `%s` does not skip already-selected categories" % (header.strip()[:60],)
        )
    return "; ".join(reasons) if reasons else "settlement guard missing"


def fail(*parts):
    VIOLATIONS.append(": ".join(str(x) for x in parts))


def check(root: Path) -> None:
    p = root / REL_SCREEN
    if not p.exists():
        fail(f"{REL_SCREEN}: missing source file")
        return
    src = code_only(p.read_text(encoding="utf-8"))

    # --- arm 1: ensureLoaded wraps its map writes in a mutable snapshot ---
    body = fn_body(src, "fun ensureLoaded(")
    if body is None:
        fail(REL_SCREEN, "ensureLoaded function not found")
    else:
        r = block_after(body, "Snapshot.withMutableSnapshot")
        if r is None:
            fail(REL_SCREEN, "ensureLoaded state-map writes are not inside Snapshot.withMutableSnapshot")
        else:
            snap = r[0]
            if "selectedMirrorId[cat]" not in snap or "useCustomMirror[cat]" not in snap:
                fail(REL_SCREEN, "ensureLoaded snapshot block does not cover both state-map writes")

    # --- arm 2: autoDetectOnceIfNeeded keeps applyMirror off the main thread ---
    detect = fn_body(src, "fun autoDetectOnceIfNeeded(")
    if detect is None:
        fail(REL_SCREEN, "autoDetectOnceIfNeeded function not found")
    else:
        if "applyMirror(" not in detect:
            fail(REL_SCREEN, "autoDetectOnceIfNeeded does not call applyMirror")
        for mb in main_blocks(detect):
            if "applyMirror(" in mb:
                fail(REL_SCREEN, "autoDetectOnceIfNeeded calls applyMirror inside withContext(Dispatchers.Main) — disk IO on the main thread")

    # --- arm 3: speed-test auto-select writes useCustomMirror + persists + applies ---
    entry = fn_body(src, "fun runAllTests(")
    if entry is None:
        fail(REL_SCREEN, "runAllTests function not found")
    else:
        # [dv25-B1] the body may live in the private shared body
        # (startAllTests) so the public entry point can stay free of the
        # auto-detect preemption it must not perform on the boot path.
        shared = fn_body(src, "private fun startAllTests(") or ""
        tail = entry + shared
        if shared:
            if "startAllTests(" not in entry:
                fail(REL_SCREEN, "runAllTests does not reach the shared speed-test body (startAllTests)")
        if "useCustomMirror[cat] = true" not in tail:
            fail(REL_SCREEN, "runAllTests auto-select does not set useCustomMirror[cat] = true (speed-test conclusion never takes effect)")
        if "persist(context, cat)" not in tail:
            fail(REL_SCREEN, "runAllTests auto-select does not persist the selection")
        if "applyMirror(" not in tail:
            fail(REL_SCREEN, "runAllTests never applies the auto-selected mirror")
        for mb in main_blocks(tail):
            if "applyMirror(" in mb:
                fail(REL_SCREEN, "runAllTests calls applyMirror inside withContext(Dispatchers.Main) — disk IO on the main thread")

    # --- arm 4: retryApkWorld wraps the ALPINE writes in a mutable snapshot ---
    body = fn_body(src, "suspend fun retryApkWorldWithMirrorFallback(")
    if body is None:
        fail(REL_SCREEN, "retryApkWorldWithMirrorFallback function not found")
    else:
        r = block_after(body, "Snapshot.withMutableSnapshot")
        if r is None:
            fail(REL_SCREEN, "retryApkWorldWithMirrorFallback state-map writes are not inside Snapshot.withMutableSnapshot")
        else:
            snap = r[0]
            if ("selectedMirrorId[MirrorCategory.ALPINE]" not in snap
                    or "useCustomMirror[MirrorCategory.ALPINE]" not in snap):
                fail(REL_SCREEN, "retryApkWorld snapshot block does not cover both ALPINE writes")

    # --- arm 5: the settlement preserves a user selection ---
    if detect is not None:
        reason = settlement_guard_ok(src, detect)
        if reason:
            fail(REL_SCREEN, f"autoDetectOnceIfNeeded: {reason} — a first-boot settlement would overwrite the mirror the user picked by hand")

        # --- arm 6: the detector owns a cancellable slot the user entry points cancel ---
        slot, local = launch_slot(detect)
        if slot is None:
            fail(REL_SCREEN, "autoDetectOnceIfNeeded launches with no ownership slot (bare scope.launch) — cancelling the inner run leaves the settlement alive")
        else:
            if not re.search(r"@Volatile\s+private\s+var\s+" + re.escape(slot) + r"\b", src):
                fail(REL_SCREEN, f"the auto-detect job slot `{slot}` is not a @Volatile field")
            if local is not None and f"{local}.start()" not in detect:
                fail(REL_SCREEN, f"the auto-detect job local `{local}` is never started (lazy launch without start() never runs)")
            for sig, label in (
                ("fun runAllTests(", "runAllTests"),
                ("fun runTest(", "runTest"),
            ):
                b = fn_body(src, sig)
                if b is None:
                    fail(REL_SCREEN, f"{label} function not found")
                    continue
                if f"{slot}?.cancel()" not in b:
                    fail(REL_SCREEN, f"{label} does not cancel the auto-detect job (`{slot}?.cancel()`) — a user test leaves the boot settlement running")

            # --- arm 7: settlement is ownership-checked ---
            settlement = None
            for mb in main_blocks(detect):
                if re.search(r"selectedMirrorId\[\w+\]\s*=", mb):
                    settlement = mb
                    break
            if settlement is None:
                fail(REL_SCREEN, "autoDetectOnceIfNeeded has no main-thread settlement block writing selectedMirrorId[...]")
            elif not (f"{slot} !==" in settlement or f"{slot} ===" in settlement or "isActive" in settlement):
                fail(REL_SCREEN, f"autoDetectOnceIfNeeded settles without an ownership check (`{slot} !== self` / isActive)")


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_SCREEN = '''package com.rikkaminis.app.ui.sandbox

import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.runtime.snapshots.SnapshotStateMap
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.withContext

object MirrorSpeedTestViewModel {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    @Volatile private var currentJob: Job? = null
    @Volatile private var autoDetectJob: Job? = null
    val results: SnapshotStateMap<String, List<String>> = mutableStateMapOf()
    val selectedMirrorId: SnapshotStateMap<String, String> = mutableStateMapOf()
    val useCustomMirror: SnapshotStateMap<String, Boolean> = mutableStateMapOf()
    @Volatile private var loaded = false

    fun ensureLoaded(context: Context) {
        if (loaded) return
        synchronized(this) {
            if (loaded) return
            val p = prefs(context)
            Snapshot.withMutableSnapshot {
                for (cat in MirrorCategory.entries) {
                    val savedUrl = p.getString("mirror.selected.${cat.key}", null)
                    if (savedUrl != null) selectedMirrorId[cat] = entry.id
                    useCustomMirror[cat] = p.getBoolean("mirror.useCustom.${cat.key}", false)
                }
            }
            loaded = true
        }
    }

    fun runAllTests(context: Context) {
        autoDetectJob?.cancel()
        startAllTests(context)
    }

    private fun startAllTests(context: Context) {
        currentJob?.cancel()
        val job = scope.launch(start = CoroutineStart.LAZY) {
            val self = coroutineContext[Job]
            val autoApplied = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
                if (currentJob !== self) return@withContext
                results.clear()
                results.putAll(sorted)
                for ((cat, fastest) in autoSelectFastest(sorted) { selectedMirrorId[it] != null }) {
                    selectedMirrorId[cat] = fastest.mirror.id
                    useCustomMirror[cat] = true
                    persist(context, cat)
                    autoApplied += cat
                }
            }
            for (cat in autoApplied) applyMirror(context, cat)
        }
        currentJob = job
        job.start()
    }

    fun runTest(context: Context, category: MirrorCategory) {
        autoDetectJob?.cancel()
        currentJob?.cancel()
        currentJob = scope.launch {
            val self = coroutineContext[Job]
            val autoApplied = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
                results[category] = sorted
                for ((cat, fastest) in autoSelectFastest(mapOf(category to sorted)) { selectedMirrorId[it] != null }) {
                    selectedMirrorId[cat] = fastest.mirror.id
                    useCustomMirror[cat] = true
                    persist(context, cat)
                    autoApplied += cat
                }
            }
            for (cat in autoApplied) applyMirror(context, cat)
        }
    }

    fun autoDetectOnceIfNeeded(context: Context) {
        ensureLoaded(context)
        autoDetectJob?.cancel()
        val job = scope.launch(start = CoroutineStart.LAZY) {
            val self = coroutineContext[Job]
            startAllTests(context)
            currentJob?.join()
            val toApply = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
                if (autoDetectJob !== self) return@withContext
                val alreadySelected = selectedMirrorId.keys.toSet()
                for ((cat, fastest) in decideAutoApply(results.toMap(), alreadySelected)) {
                    selectedMirrorId[cat] = fastest.mirror.id
                    useCustomMirror[cat] = true
                    persist(context, cat)
                    toApply += cat
                }
            }
            for (cat in toApply) applyMirror(context, cat)
        }
        autoDetectJob = job
        job.start()
    }

    suspend fun retryApkWorldWithMirrorFallback(
        context: Context,
        attemptOnce: suspend () -> Boolean,
    ): Boolean {
        ensureLoaded(context)
        var ok = attemptOnce()
        if (ok) return true
        Snapshot.withMutableSnapshot {
            selectedMirrorId[MirrorCategory.ALPINE] = next.id
            useCustomMirror[MirrorCategory.ALPINE] = true
        }
        persist(context, MirrorCategory.ALPINE)
        applyMirror(context, MirrorCategory.ALPINE)
        return ok
    }
}

internal fun autoSelectFastest(
    sorted: Map<MirrorCategory, List<MirrorTestResult>>,
    hasPriorSelection: (MirrorCategory) -> Boolean,
): List<Pair<MirrorCategory, MirrorTestResult>> =
    MirrorCategory.entries.mapNotNull { cat ->
        if (hasPriorSelection(cat)) return@mapNotNull null
        sorted[cat]?.firstOrNull { it.isSuccess }?.let { cat to it }
    }

internal fun decideAutoApply(
    results: Map<MirrorCategory, List<MirrorTestResult>>,
    alreadySelected: Set<MirrorCategory>,
): List<Pair<MirrorCategory, MirrorTestResult>> =
    autoSelectFastest(results) { it in alreadySelected }
        .filterNot { (_, fastest) -> fastest.mirror.isOfficial }
'''


def write_fixture(root: Path, screen: str) -> None:
    p = root / REL_SCREEN
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(screen, encoding="utf-8")


def mutate(fixture: str, old: str, new: str, label: str) -> str:
    """Exact single-occurrence replacement; a miss is a selftest failure, not a
    silently skipped arm."""
    if fixture.count(old) != 1:
        sys.exit(f"MUTATION ANCHOR BROKEN ({label}): expected exactly 1 occurrence, got {fixture.count(old)}")
    return fixture.replace(old, new)


def baseline_fixture() -> str:
    """The shipped (pre-dv25-B1) shape: bare `scope.launch`, no ownership slot,
    and a settlement that walks every category unconditionally. Arms 1-4 must
    stay green on it so the two new arms own the red."""
    f = FIXTURE_SCREEN
    f = mutate(f, "    fun runAllTests(context: Context) {\n        autoDetectJob?.cancel()\n",
               "    fun runAllTests(context: Context) {\n", "baseline-runAllTests")
    f = mutate(f, "        autoDetectJob?.cancel()\n        val job = scope.launch(start = CoroutineStart.LAZY) {\n            val self = coroutineContext[Job]\n            startAllTests(context)",
               "        scope.launch(start = CoroutineStart.LAZY) {\n            val self = coroutineContext[Job]\n            runAllTests(context)", "baseline-launch")
    f = mutate(f, "            for (cat in toApply) applyMirror(context, cat)\n        }\n        autoDetectJob = job\n        job.start()\n    }",
               "            for (cat in toApply) applyMirror(context, cat)\n        }\n    }", "baseline-tail")
    f = mutate(f, "                if (autoDetectJob !== self) return@withContext\n                val alreadySelected = selectedMirrorId.keys.toSet()\n                for ((cat, fastest) in decideAutoApply(results.toMap(), alreadySelected)) {",
               "                for (cat in MirrorCategory.entries) {\n                    val fastest = results[cat]?.firstOrNull { it.isSuccess } ?: continue\n                    if (fastest.mirror.isOfficial) continue",
               "baseline-settlement")
    return f


def selftest() -> None:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def run(label: str, screen: str, expect: tuple):
            nonlocal ok
            write_fixture(root, screen)
            VIOLATIONS.clear()
            check(root)
            got = tuple(sorted(VIOLATIONS))
            if len(expect) and not all(any(x in v for v in VIOLATIONS) for x in expect):
                ok = False
                print(f"SELFTEST {label}: expected red on {expect}, got {got}")
            elif len(expect) == 0 and VIOLATIONS:
                ok = False
                print(f"SELFTEST {label}: expected GREEN, got {got}")
            else:
                print(f"SELFTEST {label}: {'RED as expected' if expect else 'GREEN'}")

        # A: the shipped post-fix shape must pass every arm
        run("A (compliant)", FIXTURE_SCREEN, ())

        # V1: drop useCustomMirror write from the auto-select -> arm 3 red
        run("V1 (drop useCustomMirror)", mutate(
            FIXTURE_SCREEN,
            "                    useCustomMirror[cat] = true\n                    persist(context, cat)\n                    autoApplied += cat\n                }\n            }\n            for (cat in autoApplied) applyMirror(context, cat)\n        }\n        currentJob = job",
            "                    persist(context, cat)\n                    autoApplied += cat\n                }\n            }\n            for (cat in autoApplied) applyMirror(context, cat)\n        }\n        currentJob = job", "V1"),
            ("useCustomMirror[cat] = true",))

        # V2: put applyMirror back inside the auto-detection Main block -> arm 2 red
        run("V2 (applyMirror in Main)", mutate(
            FIXTURE_SCREEN,
            "                    toApply += cat\n                }",
            "                    toApply += cat\n                    applyMirror(context, cat)\n                }", "V2"),
            ("autoDetectOnceIfNeeded calls applyMirror inside",))

        # V3: remove the snapshot transaction from ensureLoaded -> arm 1 red
        run("V3 (ensureLoaded unwrapped)", mutate(
            FIXTURE_SCREEN,
            "Snapshot.withMutableSnapshot {\n                for (cat in MirrorCategory.entries)",
            "for (cat in MirrorCategory.entries)", "V3"),
            ("ensureLoaded",))

        # V4: remove the snapshot transaction from retryApkWorld -> arm 4 red
        run("V4 (retryApkWorld unwrapped)", mutate(
            FIXTURE_SCREEN,
            "Snapshot.withMutableSnapshot {\n            selectedMirrorId[MirrorCategory.ALPINE] = next.id\n            useCustomMirror[MirrorCategory.ALPINE] = true\n        }",
            "selectedMirrorId[MirrorCategory.ALPINE] = next.id\n        useCustomMirror[MirrorCategory.ALPINE] = true", "V4"),
            ("retryApkWorldWithMirrorFallback",))

        # V5: the settlement decision stops consulting the user selection -> arm 5 red
        run("V5 (guard dropped)", mutate(
            FIXTURE_SCREEN,
            "autoSelectFastest(results) { it in alreadySelected }",
            "autoSelectFastest(results) { false }", "V5"),
            ("does not skip already-selected categories",))

        # V5b: the rejection is dropped from the decision hop -> arm 5 red
        run("V5b (rejection dropped)", mutate(
            FIXTURE_SCREEN,
            "MirrorCategory.entries.mapNotNull { cat ->\n        if (hasPriorSelection(cat)) return@mapNotNull null\n        sorted[cat]?.firstOrNull { it.isSuccess }?.let { cat to it }\n    }",
            "MirrorCategory.entries.mapNotNull { cat ->\n        sorted[cat]?.firstOrNull { it.isSuccess }?.let { cat to it }\n    }", "V5b"),
            ("does not skip already-selected categories",))

        # V6: back to a bare scope.launch (no ownership slot) -> arm 6 red
        bare = mutate(
            FIXTURE_SCREEN,
            "        autoDetectJob?.cancel()\n        val job = scope.launch(start = CoroutineStart.LAZY) {\n            val self = coroutineContext[Job]\n            startAllTests(context)",
            "        scope.launch(start = CoroutineStart.LAZY) {\n            val self = coroutineContext[Job]\n            startAllTests(context)", "V6a")
        bare = mutate(
            bare,
            "            for (cat in toApply) applyMirror(context, cat)\n        }\n        autoDetectJob = job\n        job.start()\n    }",
            "            for (cat in toApply) applyMirror(context, cat)\n        }\n    }", "V6b")
        run("V6 (bare launch)", bare, ("no ownership slot",))

        # V7: the single-category entry point stops cancelling detection -> arm 6 red
        run("V7 (runTest forgets cancel)", mutate(
            FIXTURE_SCREEN,
            "    fun runTest(context: Context, category: MirrorCategory) {\n        autoDetectJob?.cancel()\n",
            "    fun runTest(context: Context, category: MirrorCategory) {\n", "V7"),
            ("runTest does not cancel the auto-detect job",))

        # V8: the settlement loses its ownership check -> arm 7 red
        run("V8 (ownership check dropped)", mutate(
            FIXTURE_SCREEN,
            "                if (autoDetectJob !== self) return@withContext\n",
            "", "V8"),
            ("settles without an ownership check",))

        # V9: the shipped baseline shape -> arms 5+6 red, arms 1-4 stay green
        run("V9 (baseline shape)", baseline_fixture(),
            ("no ownership slot", "does not skip already-selected categories"))

        # V10: the slot is not volatile -> arm 6 red
        run("V10 (slot not volatile)", mutate(
            FIXTURE_SCREEN,
            "    @Volatile private var autoDetectJob: Job? = null",
            "    private var autoDetectJob: Job? = null", "V10"),
            ("is not a @Volatile field",))

    if not ok:
        sys.exit("MIRROR_VM_THREAD_SELFTEST=FAIL")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        print("MIRROR_VM_THREAD_SELFTEST=PASS")
        return
    root = Path(args[0]) if args else Path.cwd()
    if not root.exists():
        sys.exit(f"FAIL: root {root} does not exist")
    check(root)
    if VIOLATIONS:
        print(f"FAIL: {len(VIOLATIONS)} violation(s)")
        for v in VIOLATIONS:
            print(f"  {v}")
        sys.exit(1)
    print("MIRROR_VM_THREAD_CHECK=PASS")


if __name__ == "__main__":
    main()
