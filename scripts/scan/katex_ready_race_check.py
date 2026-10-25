#!/usr/bin/env python3
"""deep-audit-1019 ISSUE-07 (P1) — KaTeX ready-gate & alias read-path lock gate.

Pins the two race fixes from branch fix/da1019-katex-store-race:

A. KatexWebViewPool.kt — "page finished" / "waiter" coordination must be a
   single atomic step (pre-fix: `awaitReady()` did an UNLOCKED
   `if (isReady) return true` followed by a SEPARATE unlocked deferred
   registration, so a main-thread onPageFinished landing in between completed
   nothing and stranded the waiter for the full 4 s budget).
   The fix routes the coordination through `KatexReadyGate` — pinned here are
   its load-bearing shapes:
     A1  `onPageFinished` (the main-thread entry) calls `readyGate.markPageFinished()`
     A2  the pool no longer juggles its own `isReady` / `readyDeferred` fields
         (any surviving direct flip = the old unlocked shape creeping back);
     A3  `markPageFinished` flips the ready flag and takes the waiter inside
         ONE `synchronized(lock)` block, completing OUTSIDE the lock;
     A4  `await` registers/reads the waiter under the same lock.

B. ChatViewModelStore.kt — the alias table read path must share the writers'
   monitor (pre-fix: writers `@Synchronized`, `activeSessionId` getter +
   `resolveKey` unlocked; the getter is read from offload worker threads via
   `minis-config session.*` → ConfigBuiltins' sessionIdProvider).
     B1  `resolveKey` (the single aliases lookup) is `@Synchronized`;
     B2  `resolveKey` still reads `aliases[sessionId]` (the lookup wasn't
         renamed away);
     B3  the `activeSessionId` getter is `@Synchronized`;
     B4  the read path must not be "fixed" by deleting the lookup from the
         getter (a getter that never touches aliases re-opens the draft-key
         miss).

Comment-stripped before matching so KDoc mentions of the pre-fix shape
(this gate's own documentation) cannot satisfy a criterion.

Usage: python3 katex_ready_race_check.py [ROOT]   (ROOT = repo root, default cwd)
       python3 katex_ready_race_check.py --selftest
Exit:  0 = gate green, 1 = violations / selftest fail, 2 = usage error.
"""
import re
import sys
import tempfile
from pathlib import Path

KATEX = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/KatexWebViewPool.kt"
STORE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelStore.kt"

SELFTEST_MARKER = "KATEX_READY_RACE_SELFTEST=PASS"

CRIT_A1 = "A1 onPageFinished routes through readyGate.markPageFinished()"
CRIT_A2 = "A2 pool has no direct isReady/readyDeferred field juggling left"
CRIT_A3 = "A3 markPageFinished flips ready + takes waiter in ONE synchronized block, completes outside"
CRIT_A4 = "A4 gate.await() reads the waiter under the same lock"
CRIT_B1 = "B1 ChatViewModelStore.resolveKey is @Synchronized"
CRIT_B2 = "B2 resolveKey still reads aliases[sessionId]"
CRIT_B3 = "B3 ChatViewModelStore.activeSessionId getter is @Synchronized"
CRIT_B4 = "B4 activeSessionId getter still resolves through resolveKey"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def find_block(text: str, header_re: str) -> str | None:
    """Source of the (first) top-level/matchable block whose header matches."""
    m = re.search(header_re, text)
    if not m:
        return None
    start = m.start()
    depth = 0
    seen = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
            seen = True
        elif ch == "}":
            depth -= 1
            if seen and depth <= 0:
                return text[start:i + 1]
    return text[start:]


def line_of(text: str, needle: str) -> int:
    pos = text.find(needle)
    return text[:pos].count("\n") + 1 if pos >= 0 else -1


# ── wide-1024 helpers ─────────────────────────────────────────────────────
# Four blind spots in this gate, all the same shape: the gate asserted that a
# token existed, not that the value it computes is the one used.
ANNOTATION_ONLY_RE = re.compile(r"^@[A-Za-z_][\w.]*(?:\([^()]*\))?$")


def enclosing_heads(src: str, idx: int):
    """Heads of every `{ … }` block enclosing [idx], innermost first."""
    heads = []
    depth = 0
    i = idx
    while i >= 0:
        c = src[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                j = i - 1
                while j >= 0 and src[j] in " \t\n":
                    j -= 1
                end = j + 1
                while j >= 0 and src[j] not in "\n;{}":
                    j -= 1
                heads.append(src[j + 1:end].strip())
            else:
                depth -= 1
        i -= 1
    return heads


def conditional_context(src: str, idx: int):
    """Head of the enclosing conditional (`if (…)`, `else`, `while (…)`,
    `when`) that gates the statement at [idx], else None."""
    for head in enclosing_heads(src, idx):
        if re.match(r"^(?:if|else|while|when|until)\b", head) or head.startswith("else"):
            return head
    line_start = src.rfind("\n", 0, idx) + 1
    prefix = src[line_start:idx]
    if re.search(r"\b(?:if|while)\s*\([^)]*\)\s*$", prefix):
        return prefix.strip()
    return None


def in_discarded_lambda(src: str, idx: int) -> bool:
    """`x.also { … }` / `x.apply { … }` hand their receiver on and throw the
    block's value away."""
    for head in enclosing_heads(src, idx):
        if head.endswith(".also") or head.endswith(".apply"):
            return True
    return False


def annotation_only_lines_above(text: str, decl_idx: int):
    """Annotation lines attached to the declaration at [decl_idx].

    A line counts only when it carries NOTHING but annotations — the anchor
    drift this closes is `@Synchronized private fun noopLock() {}` sitting in
    the 80-char window above an unlocked `resolveKey`. A real modifier line or
    another function body terminates the search.
    """
    prefix = text[:decl_idx]
    lines = prefix.split("\n")[:-1]
    anns = []
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if ANNOTATION_ONLY_RE.match(s):
            anns.append(s)
            continue
        break
    return anns



def analyse(katex_code: str, store_code: str) -> list[tuple[str, bool, str, int]]:
    """katex_code / store_code = comment-stripped sources of the two files."""
    out: list[tuple[str, bool, str, int]] = []

    # ---- A1: onPageFinished entry routes through the gate ----
    opf = find_block(katex_code, r"override fun onPageFinished\([^)]*\)")
    if opf is None:
        out.append((CRIT_A1, False, "onPageFinished not found", -1))
    elif "readyGate.markPageFinished()" not in opf:
        out.append((CRIT_A1, False, "onPageFinished does not call readyGate.markPageFinished()", -1))
    else:
        out.append((CRIT_A1, True, "", line_of(katex_code, "readyGate.markPageFinished()")))

    # ---- A2: the pool's own unlocked flag/deferred juggling is gone ----
    bad_tokens = [
        ("isReady = true", "direct ready-flag flip"),
        ("isReady = false", "direct ready-flag clear"),
        ("readyDeferred?.complete", "direct deferred completion"),
        ("readyDeferred = null", "direct deferred clearing"),
        ("private var isReady", "pool-level ready field"),
        ("private var readyDeferred", "pool-level deferred field"),
    ]
    a2_bad = [(tok, why) for tok, why in bad_tokens if tok in katex_code]
    if a2_bad:
        first = a2_bad[0]
        out.append((CRIT_A2, False, f"{first[1]} present: {first[0]!r}", line_of(katex_code, first[0])))
    else:
        out.append((CRIT_A2, True, "", -1))

    # ---- A3: gate.markPageFinished shape ----
    mpf = find_block(katex_code, r"fun markPageFinished\(\)")
    if mpf is None:
        out.append((CRIT_A3, False, "KatexReadyGate.markPageFinished not found", -1))
    else:
        sync = re.search(r"val d = synchronized\(lock\) \{\s*ready = true\s*waiter\s*\}", mpf)
        # [wide-1024 A3] the captured waiter must actually be completed on the
        # settlement path: `if (!ready) d?.complete(Unit)` sits right where the
        # old arm looked and can never run (ready was just set true inside the
        # lock), so every pre-ready render times out.
        done_ok, done_detail = False, "the waiter taken under the lock is never completed"
        cap = re.search(r"val\s+([A-Za-z_]\w*)\s*=\s*synchronized\s*\(\s*lock\s*\)\s*\{", mpf)
        if cap:
            name = cap.group(1)
            null_check = re.compile(
                r"^if\s*\(\s*(?:" + re.escape(name) + r"\s*!==?\s*null|null\s*!==?\s*"
                + re.escape(name) + r")\s*\)$")
            for m in re.finditer(re.escape(name) + r"\s*\??\.\s*complete\s*\(", mpf):
                guard = conditional_context(mpf, m.start())
                if guard and null_check.match(guard.strip()):
                    guard = None  # `if (d != null) d.complete(…)` == `d?.complete(…)`
                if guard:
                    done_detail = ("the completion `%s` sits under `%s` — a guarded completion "
                                   "skips exactly the requests that were already waiting"
                                   % (m.group(0).strip(), guard[:60]))
                    continue
                done_ok = True
                break
        ok = bool(sync) and done_ok
        if not ok:
            out.append((CRIT_A3, False,
                        "flag flip + waiter take not one atomic step (or completion inside the "
                        "lock / conditional): " + done_detail, -1))
        else:
            out.append((CRIT_A3, True, "", line_of(katex_code, "fun markPageFinished()")))

    # ---- A4: gate.await registers/reads the waiter under the lock ----
    await_fn = find_block(katex_code, r"suspend fun await\(timeoutMs: Long\)")
    if await_fn is None:
        out.append((CRIT_A4, False, "KatexReadyGate.await not found", -1))
    else:
        sync = re.search(
            r"val d = synchronized\(lock\) \{\s*if \(ready\) return null\s*"
            r"waiter \?: CompletableDeferred<Unit>\(\)\.also \{ waiter = it \}",
            await_fn,
        )
        # [wide-1024 A4] the value AWAITED must be the deferred taken/registered
        # under the lock. `withTimeoutOrNull(ms) { CompletableDeferred<Unit>().await() }`
        # keeps every token the old arm matched and waits on a deferred nobody
        # will ever complete — a guaranteed timeout.
        awaited_ok, awaited_detail = False, "the timeout body awaits something other than the locked deferred"
        cap = re.search(r"val\s+([A-Za-z_]\w*)\s*=\s*synchronized\s*\(\s*lock\s*\)\s*\{", await_fn)
        if cap:
            for m in re.finditer(r"withTimeoutOrNull\s*\(([^)]*)\)\s*\{([^{}]*)\}", await_fn):
                if re.search(r"\b" + re.escape(cap.group(1)) + r"\s*\.\s*await\s*\(", m.group(2)):
                    awaited_ok = True
                    break
                awaited_detail = ("the timeout body awaits `%s` instead of the `%s` produced "
                                  "under the lock" % (" ".join(m.group(2).split())[:70],
                                                      cap.group(1)))
        ok = bool(sync) and awaited_ok
        out.append((CRIT_A4, ok, "" if ok else "check-then-act not under one lock in await()"
                    if not sync else awaited_detail, -1))

    # ---- B1/B2: resolveKey locked + still a real lookup ----
    rk = find_block(store_code, r"private fun resolveKey\(sessionId: String\)")
    if rk is None:
        out.append((CRIT_B1, False, "resolveKey not found", -1))
        out.append((CRIT_B2, False, "resolveKey not found", -1))
    else:
        decl_idx = store_code.find("private fun resolveKey(sessionId: String)")
        line_start = store_code.rfind("\n", 0, decl_idx) + 1
        own_line = store_code[line_start:decl_idx]
        # [wide-1024 B1] the annotation must be attached to THIS declaration.
        # The old arm read a fixed 3-line window, so `@Synchronized private fun
        # noopLock() {}` one line above made an UNLOCKED resolveKey look locked.
        anns = annotation_only_lines_above(store_code, decl_idx)
        annotated = ("@Synchronized" in own_line
                     or any("@Synchronized" in a for a in anns))
        detail = ""
        if not annotated:
            stray = [s.strip() for s in store_code[:decl_idx].split("\n")[-6:] if "@Synchronized" in s]
            detail = ("no @Synchronized on resolveKey"
                      + (" — the annotation in the window above belongs to another declaration: `%s`"
                         % stray[-1] if stray else ""))
        out.append((CRIT_B1, annotated, detail, line_of(store_code, "private fun resolveKey(")))
        looks_up = "aliases[sessionId]" in rk
        out.append((CRIT_B2, looks_up, "" if looks_up else "resolveKey no longer reads aliases[sessionId]", -1))

    # ---- B3/B4: activeSessionId getter locked + still resolves ----
    getter = re.search(r"val activeSessionId: String\?\s*(.*?get\(\)[^\n]*\n[^\n]*)", store_code)
    if getter is None:
        out.append((CRIT_B3, False, "activeSessionId getter not found", -1))
        out.append((CRIT_B4, False, "activeSessionId getter not found", -1))
    else:
        seg = getter.group(0)
        out.append((CRIT_B3, "@Synchronized" in seg, "" if "@Synchronized" in seg else "getter not @Synchronized", -1))
        # [wide-1024 B4] the getter must PUBLISH the resolver's value: an
        # `.also { … resolveKey(it) … }` chain returns the un-resolved receiver,
        # so a draft key reaches `minis-config session.*` verbatim.
        pos = seg.find("resolveKey(")
        if pos < 0:
            out.append((CRIT_B4, False, "getter no longer resolves aliases", -1))
        elif in_discarded_lambda(seg, pos):
            out.append((CRIT_B4, False,
                        "the getter calls resolveKey inside .also/.apply, so the value it "
                        "publishes is the un-resolved key (a draft `__new__…` id reaches "
                        "minis-config verbatim)", -1))
        else:
            out.append((CRIT_B4, True, "", -1))
    return out


def scan(root: Path) -> list[str]:
    katex_path = root / KATEX
    store_path = root / STORE
    missing = [str(p) for p in (katex_path, store_path) if not p.is_file()]
    if missing:
        return [f"missing source file(s): {', '.join(missing)}"]
    katex_code = strip_comments(katex_path.read_text(encoding="utf-8"))
    store_code = strip_comments(store_path.read_text(encoding="utf-8"))
    violations = []
    for crit, ok, detail, line in analyse(katex_code, store_code):
        if not ok:
            where = f" (line ~{line})" if line > 0 else ""
            violations.append(f"{crit}{where}: {detail}")
    return violations


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


FIXED_KATEX = '''package p

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

internal class Pool {
    private val readyGate = KatexReadyGate()
    override fun onPageFinished() {
        readyGate.markPageFinished()
    }
}

internal class KatexReadyGate {
    @Volatile
    private var ready = false
    private var waiter: CompletableDeferred<Unit>? = null
    private val lock = Any()

    fun markPageFinished() {
        val d = synchronized(lock) {
            ready = true
            waiter
        }
        d?.complete(Unit)
    }

    suspend fun await(timeoutMs: Long): Boolean? {
        val d = synchronized(lock) {
            if (ready) return null
            waiter ?: CompletableDeferred<Unit>().also { waiter = it }
        }
        return withTimeoutOrNull(timeoutMs) { d.await() } == null
    }
}
'''

FIXED_STORE = '''package p

object ChatViewModelStore {
    private val aliases = mutableMapOf<String, String>()

    @Synchronized
    private fun resolveKey(sessionId: String): String =
        aliases[sessionId] ?: sessionId

    @Volatile
    private var activeSessionIdInternal: String? = null

    val activeSessionId: String?
        @Synchronized get() = activeSessionIdInternal?.let { resolveKey(it) }
}
'''


def selftest() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def write_pair(sub: str, katex: str, store: str) -> Path:
            r = root / sub
            _write(r / KATEX, katex)
            _write(r / STORE, store)
            return r

        # ---- arm 0: fixed shapes green ----
        r = write_pair("ok", FIXED_KATEX, FIXED_STORE)
        v = scan(r)
        if v:
            print(f"[selftest 0] EXPECTED green on fixed shapes, got: {v!r}")
            ok = False
        else:
            print("[selftest 0] fixed shapes green: OK")

        # ---- arm A1: onPageFinished flips the flag directly (pre-fix shape) ----
        bad = FIXED_KATEX.replace("readyGate.markPageFinished()", "isReady = true")
        r = write_pair("a1", bad, FIXED_STORE)
        v = scan(r)
        if not (any("A1" in x for x in v) and any("A2" in x for x in v)):
            print(f"[selftest A1] EXPECTED A1+A2 red, got: {v!r}")
            ok = False
        else:
            print("[selftest A1] direct onPageFinished flip red: OK")

        # ---- arm A3: split step (flag outside, waiter take outside) ----
        bad = FIXED_KATEX.replace(
            "fun markPageFinished() {\n"
            "        val d = synchronized(lock) {\n"
            "            ready = true\n"
            "            waiter\n"
            "        }\n"
            "        d?.complete(Unit)\n"
            "    }",
            "fun markPageFinished() {\n"
            "        ready = true\n"
            "        val d = waiter\n"
            "        d?.complete(Unit)\n"
            "    }",
        )
        r = write_pair("a3", bad, FIXED_STORE)
        v = scan(r)
        if not any("A3" in x for x in v):
            print(f"[selftest A3] EXPECTED A3 red, got: {v!r}")
            ok = False
        else:
            print("[selftest A3] split flag/waiter step red: OK")

        # ---- arm A4: unlocked check-then-act in await (the original defect) ----
        bad = FIXED_KATEX.replace(
            "val d = synchronized(lock) {\n"
            "            if (ready) return null\n"
            "            waiter ?: CompletableDeferred<Unit>().also { waiter = it }\n"
            "        }",
            "if (ready) return null\n"
            "        val d = waiter ?: CompletableDeferred<Unit>().also { waiter = it }",
        )
        r = write_pair("a4", bad, FIXED_STORE)
        v = scan(r)
        if not any("A4" in x for x in v):
            print(f"[selftest A4] EXPECTED A4 red, got: {v!r}")
            ok = False
        else:
            print("[selftest A4] unlocked check-then-act red: OK")

        # ---- arm B1: resolveKey unlocked (pre-fix shape) ----
        bad = FIXED_STORE.replace("    @Synchronized\n    private fun resolveKey", "    private fun resolveKey")
        r = write_pair("b1", FIXED_KATEX, bad)
        v = scan(r)
        if not any("B1" in x for x in v):
            print(f"[selftest B1] EXPECTED B1 red, got: {v!r}")
            ok = False
        else:
            print("[selftest B1] unlocked resolveKey red: OK")

        # ---- arm B3: getter unlocked ----
        bad = FIXED_STORE.replace("@Synchronized get() = activeSessionIdInternal", "get() = activeSessionIdInternal")
        r = write_pair("b3", FIXED_KATEX, bad)
        v = scan(r)
        if not any("B3" in x for x in v):
            print(f"[selftest B3] EXPECTED B3 red, got: {v!r}")
            ok = False
        else:
            print("[selftest B3] unlocked getter red: OK")

        # ---- arm B4: getter stops resolving aliases (silent re-open) ----
        bad = FIXED_STORE.replace(
            "@Synchronized get() = activeSessionIdInternal?.let { resolveKey(it) }",
            "@Synchronized get() = activeSessionIdInternal",
        )
        r = write_pair("b4", FIXED_KATEX, bad)
        v = scan(r)
        if not any("B4" in x for x in v):
            print(f"[selftest B4] EXPECTED B4 red, got: {v!r}")
            ok = False
        else:
            print("[selftest B4] non-resolving getter red: OK")

        # ---- arm C: missing file must red (not silently green) ----
        r = root / "c"
        _write(r / KATEX, FIXED_KATEX)
        v = scan(r)
        if not v or "missing" not in v[0]:
            print(f"[selftest C] EXPECTED missing-file violation, got: {v!r}")
            ok = False
        else:
            print("[selftest C] missing store file red: OK")

        # ---- wide-1024 witnesses -------------------------------------------
        def rep(text: str, old: str, new: str, label: str) -> str:
            """Exact replacement; a miss must fail the selftest, never become a
            silent no-op arm (the arm would then assert on the un-mutated
            fixture and pass for the wrong reason)."""
            if text.count(old) != 1:
                sys.exit("MUTATION ANCHOR BROKEN (%s): expected exactly 1 occurrence, got %d"
                         % (label, text.count(old)))
            return text.replace(old, new, 1)

        def expect(sub_name: str, katex: str, store: str, needle: str, want_red=True):
            nonlocal ok
            r = write_pair("w_" + sub_name.replace(" ", "_"), katex, store)
            v = scan(r)
            hit = any(needle in x for x in v)
            if want_red and not hit:
                print(f"[selftest W-{sub_name}] EXPECTED red ({needle!r}), got: {v!r}")
                ok = False
            elif not want_red and v:
                print(f"[selftest W-{sub_name}] EXPECTED GREEN, got: {v!r}")
                ok = False
            else:
                print(f"[selftest W-{sub_name}] {'red as expected' if want_red else 'green'}: OK")

        # W-A3: the waiter is taken atomically but the completion is gated on a
        # condition that is false on that very path -> every early waiter times out.
        expect("A3 (completion gated on !ready)",
               rep(FIXED_KATEX, "        d?.complete(Unit)",
                   "        if (!ready) d?.complete(Unit)", "W-A3"),
               FIXED_STORE, "A3")

        # W-A4: the timeout body awaits a brand-new deferred nobody completes.
        expect("A4 (awaits a fresh deferred)",
               rep(FIXED_KATEX,
                   "withTimeoutOrNull(timeoutMs) { d.await() }",
                   "withTimeoutOrNull(timeoutMs) { CompletableDeferred<Unit>().await() }",
                   "W-A4"),
               FIXED_STORE, "A4")

        # W-B4: resolveKey lands inside `.also`, so the getter publishes the
        # un-resolved draft key while still "calling" the resolver.
        expect("B4 (resolveKey inside .also)",
               FIXED_KATEX,
               rep(FIXED_STORE,
                   "@Synchronized get() = activeSessionIdInternal?.let { resolveKey(it) }",
                   "@Synchronized get() = activeSessionIdInternal.also { it?.let { resolveKey(it) } }",
                   "W-B4"),
               "B4")

        # W-B1: the annotation in the window above belongs to another function.
        expect("B1 (annotation belongs to noopLock)",
               FIXED_KATEX,
               rep(FIXED_STORE,
                   "    @Synchronized\n    private fun resolveKey",
                   "    @Synchronized private fun noopLock() {}\n    private fun resolveKey",
                   "W-B1"),
               "B1")

        # W-A3 green: an equivalent null-check completion must not be dragged
        # into the red set by the new criterion.
        expect("A3 (null-check completion spelled out)",
               rep(FIXED_KATEX, "        d?.complete(Unit)",
                   "        if (d != null) d.complete(Unit)", "W-A3-green"),
               FIXED_STORE, "", want_red=False)

        # W-B4 green: publishing the resolved value through a `?.let` chain.
        expect("B4 (resolved value via let)",
               FIXED_KATEX,
               rep(FIXED_STORE,
                   "@Synchronized get() = activeSessionIdInternal?.let { resolveKey(it) }",
                   "@Synchronized get() = activeSessionIdInternal?.let { raw -> resolveKey(raw) }",
                   "W-B4-green"),
               "", want_red=False)

    if ok:
        print(SELFTEST_MARKER)
        return 0
    print("KATEX_READY_RACE_SELFTEST=FAIL")
    return 1


def main(argv: list[str]) -> int:
    root: Path | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--selftest":
            return selftest()
        if arg == "--root":
            i += 1
            if i >= len(argv):
                print("usage: --root needs a value", file=sys.stderr)
                return 2
            root = Path(argv[i])
        elif not arg.startswith("--"):
            root = Path(arg)
        i += 1
    if root is None:
        root = Path.cwd()
    if not root.is_dir():
        print(f"repo root not found: {root}", file=sys.stderr)
        return 2

    violations = scan(root)
    if violations:
        print("[katex-ready-race] VIOLATIONS FOUND:")
        for v in violations:
            print(f"  FAIL: {v}")
        print(
            "Fix: keep onPageFinished/waiter coordination inside KatexReadyGate's "
            "single synchronized block, and the alias read path "
            "(resolveKey + activeSessionId getter) under ChatViewModelStore's monitor."
        )
        return 1
    print("[katex-ready-race] clean: gate coordination atomic + alias read path locked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
