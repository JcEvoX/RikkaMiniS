#!/usr/bin/env python3
"""da1022-F gate: MirrorSpeedTestViewModel cross-thread snapshot writes,
main-thread IO, and speed-test auto-select wiring.

Usage (repo root, positional arg supported):
    python3 scripts/scan/mirror_vm_thread_check.py [ROOT]

PASS  = exit 0, prints MIRROR_VM_THREAD_CHECK=PASS
        (--selftest prints MIRROR_VM_THREAD_SELFTEST=PASS)
FAIL  = exit 1, prints violating arm descriptions

Judged invariants (fix for deep-audit-1022 ISSUE-06; all anchors are CODE
anchors on MirrorSettingsScreen.kt — comment anchors are stripped first):
  1. ensureLoaded: the SnapshotStateMap writes (selectedMirrorId/useCustomMirror)
     must sit inside a `Snapshot.withMutableSnapshot { }` transaction — the
     function is entered from background threads (boot path) and the batch
     must land atomically.
  2. autoDetectOnceIfNeeded: `applyMirror(` must NOT appear inside any
     `withContext(Dispatchers.Main)` block (it is disk IO), and must be
     called in the function body.
  3. runAllTests auto-select: the auto-select decision must write
     `useCustomMirror[cat] = true` and `persist(context, cat)`, and
     `applyMirror(` must be called in the body but NOT inside a Main block
     (applied categories are collected and applied off the main thread).
  4. retryApkWorldWithMirrorFallback: the two ALPINE snapshot-state writes
     must sit inside a `Snapshot.withMutableSnapshot { }` transaction.
"""
import sys
import tempfile
from pathlib import Path

REL_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorSettingsScreen.kt"

VIOLATIONS = []


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
    body = fn_body(src, "fun autoDetectOnceIfNeeded(")
    if body is None:
        fail(REL_SCREEN, "autoDetectOnceIfNeeded function not found")
    else:
        if "applyMirror(" not in body:
            fail(REL_SCREEN, "autoDetectOnceIfNeeded does not call applyMirror")
        for mb in main_blocks(body):
            if "applyMirror(" in mb:
                fail(REL_SCREEN, "autoDetectOnceIfNeeded calls applyMirror inside withContext(Dispatchers.Main) — disk IO on the main thread")

    # --- arm 3: runAllTests auto-select writes useCustomMirror + persists + applies ---
    body = fn_body(src, "fun runAllTests(")
    if body is None:
        fail(REL_SCREEN, "runAllTests function not found")
    else:
        if "useCustomMirror[cat] = true" not in body:
            fail(REL_SCREEN, "runAllTests auto-select does not set useCustomMirror[cat] = true (speed-test conclusion never takes effect)")
        if "persist(context, cat)" not in body:
            fail(REL_SCREEN, "runAllTests auto-select does not persist the selection")
        if "applyMirror(" not in body:
            fail(REL_SCREEN, "runAllTests never applies the auto-selected mirror")
        for mb in main_blocks(body):
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


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_SCREEN = '''package com.rikkaminis.app.ui.sandbox

import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.runtime.snapshots.SnapshotStateMap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

object MirrorSpeedTestViewModel {
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
        currentJob?.cancel()
        currentJob = scope.launch {
            val autoApplied = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
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
    }

    fun autoDetectOnceIfNeeded(context: Context) {
        ensureLoaded(context)
        scope.launch {
            runAllTests(context)
            currentJob?.join()
            val toApply = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
                for (cat in MirrorCategory.entries) {
                    val fastest = results[cat]?.firstOrNull { it.isSuccess } ?: continue
                    selectedMirrorId[cat] = fastest.mirror.id
                    useCustomMirror[cat] = true
                    persist(context, cat)
                    toApply += cat
                }
            }
            for (cat in toApply) applyMirror(context, cat)
        }
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
'''


def write_fixture(root: Path, screen: str) -> None:
    p = root / REL_SCREEN
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(screen, encoding="utf-8")


def selftest() -> None:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # A: compliant fixture must pass
        write_fixture(root, FIXTURE_SCREEN)
        VIOLATIONS.clear()
        check(root)
        if VIOLATIONS:
            ok = False
            print(f"SELFTEST compliant fixture unexpectedly failed: {VIOLATIONS}")
        else:
            print("SELFTEST A (compliant fixture) GREEN")

        # V1: drop useCustomMirror write from runAllTests auto-select -> arm 3 red
        write_fixture(root, FIXTURE_SCREEN.replace(
            "                    useCustomMirror[cat] = true\n                    persist(context, cat)\n                    autoApplied += cat\n                }\n            }\n            for (cat in autoApplied) applyMirror(context, cat)\n        }\n    }\n\n    fun autoDetectOnceIfNeeded(",
            "                    persist(context, cat)\n                    autoApplied += cat\n                }\n            }\n            for (cat in autoApplied) applyMirror(context, cat)\n        }\n    }\n\n    fun autoDetectOnceIfNeeded(",
        ))
        VIOLATIONS.clear()
        check(root)
        if not any("useCustomMirror[cat] = true" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST V1 (drop useCustomMirror in auto-select) did not trip arm 3")
        else:
            print("SELFTEST V1 (drop useCustomMirror in auto-select) RED as expected")

        # V2: put applyMirror back inside the Main block of autoDetect -> arm 2 red
        write_fixture(root, FIXTURE_SCREEN.replace(
            "                    toApply += cat\n                }\n            }\n            for (cat in toApply) applyMirror(context, cat)",
            "                    toApply += cat\n                    applyMirror(context, cat)\n                }\n            }\n            for (cat in toApply) applyMirror(context, cat)",
        ))
        VIOLATIONS.clear()
        check(root)
        if not any("autoDetectOnceIfNeeded calls applyMirror inside" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST V2 (applyMirror back inside Main block) did not trip arm 2")
        else:
            print("SELFTEST V2 (applyMirror back inside Main block) RED as expected")

        # V3: remove the snapshot transaction from ensureLoaded -> arm 1 red
        write_fixture(root, FIXTURE_SCREEN.replace("Snapshot.withMutableSnapshot {\n                for (cat in MirrorCategory.entries)", "for (cat in MirrorCategory.entries)"))
        VIOLATIONS.clear()
        check(root)
        if not any("ensureLoaded" in v and "withMutableSnapshot" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST V3 (ensureLoaded unwrapped) did not trip arm 1")
        else:
            print("SELFTEST V3 (ensureLoaded unwrapped) RED as expected")

        # V4: remove the snapshot transaction from retryApkWorld -> arm 4 red
        write_fixture(root, FIXTURE_SCREEN.replace(
            "Snapshot.withMutableSnapshot {\n            selectedMirrorId[MirrorCategory.ALPINE] = next.id\n            useCustomMirror[MirrorCategory.ALPINE] = true\n        }",
            "selectedMirrorId[MirrorCategory.ALPINE] = next.id\n        useCustomMirror[MirrorCategory.ALPINE] = true",
        ))
        VIOLATIONS.clear()
        check(root)
        if not any("retryApkWorldWithMirrorFallback" in v and "withMutableSnapshot" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST V4 (retryApkWorld unwrapped) did not trip arm 4")
        else:
            print("SELFTEST V4 (retryApkWorld unwrapped) RED as expected")

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
