#!/usr/bin/env python3
"""
Execution-gate guard — deep-audit-1016 FIX-D.

Why this gate exists
--------------------
`sandbox/ExecutionCoordinator.kt` acquires two semaphores to serialize shell
work: a process-wide heavy gate (`Semaphore(1, true)`) and a per-process global
slot (`Semaphore(MAX_CONCURRENT_SHELLS, true)`). Two properties of that code are
invisible to a compiler, to the existing unit tests and to a code reviewer
reading only a diff, yet both fail silently:

  1. INTERRUPTIBILITY (4b). Both waits used `Semaphore.tryAcquire(timeout, unit)`
     directly. That is a blocking JUC call with no suspension point, so a
     coroutine cancelled while parked in it stayed parked — the shell stayed
     reserved for up to the timeout and the session's next command reported
     "System busy". The fix routes every blocking acquire through
     `runInterruptible(Dispatchers.IO)`. Deleting that wrapper restores the bug
     with zero compile errors.

  2. PERMIT ROLLBACK (4a). Between taking the heavy permit and entering the
     `try` whose `finally` releases it there is exactly one throwing statement:
     the global-slot acquire. Before 4b nothing could throw there. Once the wait
     became interruptible, an interrupt on that line propagates out of `execute`
     still holding the ONE heavy permit, and a leaked heavy permit queues every
     HEAVY command in the process until it dies.

  3. THREAD OF THE RECOVERY LOOP (4c). `postRecycleMemoryRecovery` runs up to
     three `System.gc()` rounds with a 120ms settle each. It is reachable
     straight from the main thread (Compose -> promoteDraftIfNeeded ->
     viewModelScope(Main.immediate) -> ensureSession -> migrateDraftResources ->
     sessionDidTerminate, and ViewModel.onCleared), where it is UI-thread jank.

Criterion (mechanical, not "does it look right")
-----------------------------------------------
  G1  `tryAcquire(` appears EXACTLY twice in the file — once per interruptible
      wrapper. A third occurrence means a raw blocking acquire came back; zero
      means the anchors moved and this gate can no longer see them (that is a
      FAIL, never a silent pass — the gate's own blind spot is how a gate dies).
  G2  `awaitHeavyGatePermitInterruptibly`'s body contains `runInterruptible(`.
  G3  `awaitGlobalShellSlotInterruptibly`'s body contains `runInterruptible(`
      and delegates to `internalAcquireGlobalShellSlot(`.
  G4  `internalAcquireGlobalShellSlot`'s body contains a `catch` that releases
      the heavy permit AND rethrows (`heavyGate.release()`, `throw`).
  G5  `postRecycleMemoryRecovery` dispatches on the caller thread:
      `Looper.getMainLooper()` + `recycleDispatchMode(` + `recoveryScope.launch`,
      with the synchronous body kept in `postRecycleMemoryRecoveryNow`.
  G6  The calibrated settle `Thread.sleep(120)` is still there: the value is a
      measured tuning constant (see the KDoc it sits in), so changing it must be
      a deliberate act that also updates this gate.

Deliberately NOT checked: that `execute()` calls the wrappers by name — a rename
is visible in review, and pinning call-site text is the brittle kind of rule
that makes a gate get disabled later. The two wrapper bodies are pinned instead.

Usage: python3 scripts/scan/exec_gate_release_check.py [repo_root]
Exit:  0 = clean, 1 = violation or lost anchor
"""
import os
import re
import sys

TARGET = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/ExecutionCoordinator.kt"

MARK_RAW_ACQUIRE = "[exec-gate] raw blocking acquire"
MARK_HEAVY_WRAP = "[exec-gate] heavy-gate wait not interruptible"
MARK_GLOBAL_WRAP = "[exec-gate] global-slot wait not interruptible"
MARK_ROLLBACK = "[exec-gate] heavy permit rollback missing"
MARK_DISPATCH = "[exec-gate] main-thread recovery dispatch missing"
MARK_WIRING = "[exec-gate] wrapper wiring changed"
MARK_SETTLE = "[exec-gate] calibrated settle changed"
MARK_ANCHOR = "[exec-gate] anchor lost"


def mask(src: str) -> str:
    """Blank out comments and string-literal interiors, preserving offsets.

    Kotlin source is full of braces and parentheses inside `"${x}ms"` strings
    and inside KDoc, so balancing braces on the raw text is unreliable. Masking
    keeps every identifier reachable (what this gate asserts on) while removing
    everything that could unbalance a scan.
    """
    buf = list(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                buf[k] = " "
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if buf[k] != "\n":
                    buf[k] = " "
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            for k in range(i + 3, max(i + 3, j - 3)):
                if buf[k] != "\n":
                    buf[k] = " "
            i = j
        elif src[i] == '"':
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    j += 1
                j += 1
            for k in range(i + 1, min(j, n)):
                if buf[k] != "\n":
                    buf[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(buf)


def body_of(masked: str, signature: str) -> str:
    """Return the body of the declaration whose text contains `signature`.

    The declaration's extent is bounded FIRST (by the next declaration at the
    same or lower indentation / the closing brace of its enclosing block) and
    the braces are balanced only INSIDE that region. Doing it the other way
    round is how this gate first shipped a blind spot: an expression body with
    no braces at all (`fun f(): Boolean = g()`) has no brace of its own, so
    balancing from the first `{` after the signature ran straight into the NEXT
    declaration's braces and read that neighbour's `runInterruptible(` as this
    function's — reporting green for the exact regression it was built to catch.
    The fixture `no_interruptible` in test_scan.py now pins that shape.

    Handles block bodies (`fun f() { ... }`) and expression bodies
    (`fun f(): Boolean = runInterruptible(...) { ... }`) alike, and returns the
    brace-less expression body as-is so a missing marker is still reported.
    """
    m = re.search(re.escape(signature), masked)
    if not m:
        return ""
    line_start = masked.rfind("\n", 0, m.start()) + 1
    indent = len(masked[line_start:m.start()]) - len(masked[line_start:m.start()].lstrip())

    end = len(masked)
    pos = masked.find("\n", m.end())
    while pos != -1:
        cand_start = pos + 1
        if cand_start >= len(masked):
            break
        cand_end = masked.find("\n", cand_start)
        if cand_end == -1:
            cand_end = len(masked)
        line = masked[cand_start:cand_end]
        stripped = line.strip()
        if stripped:
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= indent and re.match(
                    r"(@|internal\b|private\b|public\b|protected\b|override\b|object\b|"
                    r"class\b|enum\b|fun\b|val\b|var\b|const\b|\})", stripped):
                end = cand_start
                break
        pos = cand_end

    region = masked[m.end():end]
    open_idx = region.find("{")
    if open_idx < 0:
        return region
    depth = 0
    for k in range(open_idx, len(region)):
        ch = region[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return region[:k + 1]
    return region


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..")
    path = os.path.join(root, TARGET)
    if not os.path.isfile(path):
        print(f"❌ {MARK_ANCHOR}: {TARGET} not found under {root}")
        return 1

    raw = open(path, encoding="utf-8").read()
    masked = mask(raw)

    findings: list[str] = []

    # --- G1: exactly two blocking acquires, both inside the wrappers ---------
    acquire_sites = [m.start() for m in re.finditer(r"tryAcquire\(", masked)]
    if len(acquire_sites) != 2:
        lines = sorted({masked[:s].count("\n") + 1 for s in acquire_sites})
        findings.append(
            f"{MARK_RAW_ACQUIRE}: expected exactly 2 `tryAcquire(` (one inside each "
            f"interruptible wrapper), found {len(acquire_sites)}"
            + (f" at lines {lines}" if lines else "")
            + ". A blocking acquire outside `runInterruptible(Dispatchers.IO)` "
              "cannot be cancelled (4b)."
        )

    heavy_wrap = body_of(masked, "suspend fun awaitHeavyGatePermitInterruptibly(")
    if not heavy_wrap:
        findings.append(f"{MARK_ANCHOR}: `awaitHeavyGatePermitInterruptibly` not found")
    elif "runInterruptible(" not in heavy_wrap:
        findings.append(
            f"{MARK_HEAVY_WRAP}: the heavy-gate wait is no longer wrapped in "
            "runInterruptible — a cancelled coroutine parks in tryAcquire for the "
            "whole timeout (4b)"
        )

    global_wrap = body_of(masked, "suspend fun awaitGlobalShellSlotInterruptibly(")
    if not global_wrap:
        findings.append(f"{MARK_ANCHOR}: `awaitGlobalShellSlotInterruptibly` not found")
    else:
        if "runInterruptible(" not in global_wrap:
            findings.append(
                f"{MARK_GLOBAL_WRAP}: the global-slot wait is no longer wrapped in "
                "runInterruptible (4b)"
            )
        if "internalAcquireGlobalShellSlot(" not in global_wrap:
            findings.append(
                f"{MARK_ROLLBACK}: the global-slot wait no longer goes through "
                "internalAcquireGlobalShellSlot, so the heavy permit taken before "
                "it is not handed back when the wait throws (4a)"
            )

    # --- G3b: the wiring the wrappers exist for -------------------------------
    # Counted, not text-pinned (comments are masked out above): each name occurs
    # exactly twice — its own declaration plus the single call on the command
    # path. This is the shape a reviewer cannot see in a diff and a JVM test
    # cannot reach: if `execute()` goes back to calling the bare helper, both
    # wrappers stay behind as dead code, every JVM arm stays green (it tests the
    # wrappers, which still work) and the regression is live.
    for name in (
        "awaitHeavyGatePermitInterruptibly(",
        "awaitGlobalShellSlotInterruptibly(",
        "internalAcquireGlobalShellSlot(",
    ):
        found = len(re.findall(re.escape(name), masked))
        if found != 2:
            findings.append(
                f"{MARK_WIRING}: `{name}` appears {found}x, expected 2 (declaration "
                "+ the single call on the command path). An interruptible wrapper "
                "is only worth having if the path goes through it."
            )

    rollback = body_of(masked, "fun internalAcquireGlobalShellSlot(")
    if not rollback:
        findings.append(f"{MARK_ANCHOR}: `internalAcquireGlobalShellSlot` not found")
    else:
        if "catch" not in rollback:
            findings.append(
                f"{MARK_ROLLBACK}: no catch clause — an interrupt while waiting for "
                "the global slot throws out of execute() holding the heavy permit (4a)"
            )
        elif "heavyGate.release()" not in rollback:
            findings.append(
                f"{MARK_ROLLBACK}: the catch no longer releases the heavy permit (4a)"
            )
        if "throw" not in rollback:
            findings.append(
                f"{MARK_ROLLBACK}: the catch no longer rethrows, which would silently "
                "turn a cancellation into a 'no slot' rejection"
            )

    wrapper = body_of(masked, "fun postRecycleMemoryRecovery(")
    if not wrapper:
        findings.append(f"{MARK_ANCHOR}: `postRecycleMemoryRecovery` not found")
    else:
        if "Looper.getMainLooper()" not in wrapper:
            findings.append(
                f"{MARK_DISPATCH}: no main-thread check — the GC loop runs inline on "
                "whatever thread calls it, including the UI thread (4c)"
            )
        if "recycleDispatchMode(" not in wrapper:
            findings.append(
                f"{MARK_DISPATCH}: the dispatch decision no longer goes through "
                "recycleDispatchMode (4c)"
            )
        if "recoveryScope.launch" not in wrapper:
            findings.append(
                f"{MARK_DISPATCH}: main-thread callers are no longer deferred to "
                "recoveryScope (4c)"
            )
    if "fun postRecycleMemoryRecoveryNow(" not in masked:
        findings.append(
            f"{MARK_ANCHOR}: `postRecycleMemoryRecoveryNow` not found — the "
            "synchronous body must stay reachable for background callers"
        )

    # --- G6: the calibrated settle value --------------------------------------
    if "Thread.sleep(120)" not in masked.replace("  ", " "):
        findings.append(
            f"{MARK_SETTLE}: `Thread.sleep(120)` is gone. It is the measured "
            "post-GC settle used by the recycle loop; changing it is a deliberate "
            "act (update this gate together with the reason)."
        )

    if findings:
        print("❌ Execution-gate guard found issues:")
        for f in findings:
            print(f"   • {f}")
        return 1

    print("✅ Execution-gate guard: 4a rollback + 4b interruptible waits + "
          "4c main-thread dispatch all wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
