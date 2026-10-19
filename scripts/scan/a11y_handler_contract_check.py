#!/usr/bin/env python3
"""CI scan gate — accessibility CLI contract (deep-audit-1017 FIX-G).

Two properties of the host-side a11y surface that neither the compiler nor a
device-free unit test can see, and that both failed silently in exactly this
shape before:

1. `MinisAccessibilityService.captureScreenshot` must hand its result over
   through `LateValueSlot`. The platform's `TakeScreenshotCallback` cannot be
   unregistered, so a callback that lands after the 5 s timeout used to still
   copy a full-screen bitmap (~10-15 MB) into a reference the caller had
   abandoned — one leaked screen-sized allocation per timed-out capture. The
   shape matters, not just the presence of a slot: the callback must ask whether
   the caller is still waiting BEFORE it allocates (a later check would only
   recycle, not avoid, the copy), the timeout branch must close the slot before
   returning, and a refused publish must leave the slot closed.

2. `AccessibilityOffloadHandler` must declare every flag it reads with
   `hasFlag(...)` in its `booleanFlags` set, and `tap text` must report the
   outcome it observed. An undeclared flag is parsed as `--key value`, so the
   search string was swallowed and every `tap text --contains X` died on
   "missing <text>"; and the tap outcome was discarded, so a node that refused
   ACTION_CLICK and a cancelled coordinate tap both answered `ok`.

3. [deep-audit-1025 ISSUE-03] `tap` / `input` / `scroll` must have exactly one
   verb list. Each group used to carry two — the verbs its dispatcher accepts and
   the verbs `statusForSubAction` could name — and all three pairs had drifted,
   so every a11y call fell through to the generic overlay label. The verbs and
   their labels now live together in `A11yStatusLabels`, keyed by the dispatched
   verb; this arm holds the two sets equal in BOTH directions (a dispatched verb
   with no label, and a label for a verb nothing dispatches, are both failures)
   and holds the status branches to rendering through that table instead of
   keeping a second copy.

4. [deep-audit-1030 ISSUE-07 裂缝A] "is our service listed as enabled?" must have
   ONE implementation. `A11yState.isServiceEnabled` kept its own
   `split(':')`/`equals(...)` matcher while `AccessibilityRecoveryManager
   .isRevokedIn` — the predicate the repair path uses — also accepted the
   framework's abbreviated `pkg/.Class` spelling. The settings screen could
   therefore report "disabled" for a service that was bound and healthy, and the
   master switch was consulted on only one of the two paths.

5. [deep-audit-1030 ISSUE-07 裂缝B] The repair-prompt slot must carry an owner
   identity and a lock. `continuation` is published by the waiting coroutine and
   taken by `respond` on the UI thread, while the timeout and cancellation paths
   tear it down; with no owner id a LATE teardown dismissed the prompt a newer
   waiter had just published, leaving that waiter parked for its full timeout.
   (In this build the a11y tools are serial, so two concurrent `awaitDecision`s
   are not reachable — the reachable interleave is `respond` vs. timeout /
   cancellation, which is what these arms pin.)

Losing any anchor FAILS loudly (exit 1) rather than passing silently — a probe
that cannot find its subject must not report success.

Usage:
    python3 scripts/scan/a11y_handler_contract_check.py <repo_root>
    python3 scripts/scan/a11y_handler_contract_check.py --selftest
"""

import os
import re
import sys

PKG = "src/android/app/src/main/java/com/rikkaminis/app"
SERVICE = f"{PKG}/accessibility/MinisAccessibilityService.kt"
HANDLER = f"{PKG}/sandbox/offload/AccessibilityOffloadHandler.kt"

# Hardware-calibrated budget (the CLI's screenshot deadline). Pinned: a refactor
# may move the code around it, never silently re-tune it.
SCREENSHOT_TIMEOUT = "timeoutMs: Long = 5_000"

# [deep-audit-1025 ISSUE-03] (dispatched group, its dispatch function, its
# status table). The dispatcher is the source of truth for which verbs exist;
# the table is the source of truth for what the capsule says; this gate holds
# them equal.
STATUS_GROUPS = (("tap", "tapSub", "TAP"), ("input", "inputSub", "INPUT"), ("scroll", "scrollSub", "SCROLL"))

MAP_ANCHOR = "val %s: Map<String, String> = mapOf("
MEMBER_STOPS = ("\n    private fun ", "\n    internal fun ", "\n    fun ", "\n}")


def code_only(text):
    """Blank comment lines (offsets preserved) so a mention in prose cannot
    satisfy a shape check — the KDoc here names `tearDown()`/`isCallerWaiting()`
    while explaining why they exist, and the first version of the unit probe was
    fooled by exactly that."""
    out = []
    for line in text.split("\n"):
        t = line.strip()
        out.append(" " * len(line) if t.startswith("//") or t.startswith("*") or t.startswith("/*") else line)
    return "\n".join(out)


def slice_body(text, anchor, stop_patterns=("    fun ", "    private fun ", "    internal fun ", "    @")):
    """Slice from `anchor` to the next member declaration (or `@` annotation) at
    member indentation. Returns None when the anchor is absent."""
    start = text.find(anchor)
    if start < 0:
        return None
    end = len(text)
    for i in range(start + len(anchor), len(text)):
        if text[i] != "\n":
            continue
        tail = text[i + 1:]
        if any(tail.startswith(p) for p in stop_patterns):
            end = i
            break
    return text[start:end]


def balanced_call(text, anchor):
    """Slice `anchor(...)` with paren balance. None when absent."""
    start = text.find(anchor)
    if start < 0:
        return None
    open_at = text.index("(", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def scan_service(raw):
    """Findings for MinisAccessibilityService.kt (empty list = clean)."""
    text = code_only(raw)
    bad = []
    body = slice_body(text, "fun captureScreenshot(")
    if body is None:
        return ["captureScreenshot() not found — the probe lost its subject"]
    if "resultRef" in body:
        bad.append("captureScreenshot still uses the abandoned resultRef hand-off "
                   "(a late callback parks a screen-sized bitmap nothing will recycle)")
    if "LateValueSlot(" not in body:
        bad.append("captureScreenshot does not hand the result over through LateValueSlot")
    guard = body.find("isCallerWaiting()")
    copy = body.find(".copy(Bitmap.Config.ARGB_8888")
    if guard < 0:
        bad.append("the screenshot callback never asks whether the caller is still waiting")
    elif copy > 0 and guard > copy:
        bad.append("isCallerWaiting() is checked after the software copy is allocated "
                   "(the 10-15 MB allocation it exists to avoid has already happened)")
    timeout = body.find("if (!done.await(")
    if timeout < 0:
        bad.append("the await(timeout) branch is gone — cannot tell whether the slot is closed")
    else:
        closed = body.find("slot.exit(", timeout)
        returns = body.find("return ", timeout)
        if closed < 0 or (returns > 0 and closed > returns):
            bad.append("the timeout branch returns without closing the slot "
                       "(a frame that already landed stays parked)")
    if SCREENSHOT_TIMEOUT not in body:
        bad.append(f"the calibrated screenshot timeout is gone ({SCREENSHOT_TIMEOUT})")

    slot = slice_body(text, "internal class LateValueSlot", ("internal class ", "class ", "internal object "))
    if slot is None:
        bad.append("LateValueSlot is gone — nothing arbitrates the late callback")
        return bad
    publish = slice_body(slot, "fun publish(")
    if publish is None:
        bad.append("LateValueSlot.publish() is gone")
    else:
        if "getAndSet(" not in publish:
            bad.append("LateValueSlot.publish no longer swaps atomically "
                       "(the caller can be handed a value it already walked away from)")
        if "compareAndSet(" not in publish:
            bad.append("a refused publish no longer clears the slot "
                       "(the payload stays reachable in a slot nobody reads)")
    exit_fn = slice_body(slot, "fun exit(")
    if exit_fn is None:
        bad.append("LateValueSlot.exit() is gone")
    elif "onDiscard(" not in exit_fn:
        bad.append("LateValueSlot.exit no longer hands over what the caller did not take")
    return bad


def declared_flags(text):
    """The literal names of `booleanFlags = setOf(...)` (comment lines skipped)."""
    block = balanced_call(text, "booleanFlags = setOf")
    if block is None:
        return None
    body = "\n".join(l for l in block.split("\n") if not l.strip().startswith("//"))
    return set(re.findall(r'"([^"]+)"', body))


def scan_handler(raw):
    """Findings for AccessibilityOffloadHandler.kt (empty list = clean)."""
    text = code_only(raw)
    bad = []
    declared = declared_flags(text)
    if declared is None:
        bad.append("the handle() booleanFlags declaration is gone")
    else:
        used = set()
        for call in re.finditer(r'hasFlag\(([^)]*)\)', text):
            used.update(re.findall(r'"([^"]+)"', call.group(1)))
        undeclared = sorted(used - declared)
        if undeclared:
            bad.append(f"flag(s) read with hasFlag() but not declared boolean: {undeclared} "
                       "(OffloadArgs consumes the next token as their value)")

    body = slice_body(text, "private fun tapText(")
    if body is None:
        bad.append("tapText() not found — the probe lost its subject")
        return bad
    if "TapTextFlow.plan(" not in body:
        bad.append("tapText no longer decides through TapTextFlow.plan "
                   "(the performAction result is discarded again)")
    if "TapTextFlow.fallbackSucceeded(" not in body:
        bad.append("tapText no longer reads the coordinate tap's own outcome")
    if 'err(args, "ACTION_FAILED"' not in body:
        bad.append("tapText cannot report a failed tap — success is assumed again")
    if "if (n.isClickable) n.performAction(" in body:
        bad.append("tapText discards performAction()'s return value again")
    return bad


def member_body(text, signature):
    """Slice from [signature] to the next member declaration or the class's
    closing brace. None when either anchor is missing (fail-closed)."""
    start = text.find(signature)
    if start < 0:
        return None
    rest = text[start + len(signature):]
    ends = [i for i in (rest.find(s) for s in MEMBER_STOPS) if i >= 0]
    if not ends:
        return None
    return text[start:start + len(signature) + min(ends)]


def dispatched_verbs(text, sub_fn):
    """The `"<verb>" ->` literals of a dispatch block. The `else ->` arm's message
    string does not match: its quote is followed by `)`, not `->`."""
    body = member_body(text, "private fun %s(" % sub_fn)
    if body is None:
        return None
    return {m.group(1) for m in re.finditer(r'"([a-z][a-z0-9-]*)"\s*->', body)}


def table_keys(text, table):
    """Keys of one `val <table>: Map<String, String> = mapOf(...)` literal, with
    paren balance so a following expression cannot leak in."""
    anchor = text.find(MAP_ANCHOR % table)
    if anchor < 0:
        return None
    open_at = text.find("(", anchor)
    depth = 0
    close = -1
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
    if close < 0:
        return None
    body = "\n".join(
        l for l in text[open_at:close].split("\n") if not l.strip().startswith("//")
    )
    keys = {m.group(1) for m in re.finditer(r'"([^"]+)"\s+to\s+"[^"]*"', body)}
    return keys or None


def scan_status_tables(raw):
    """Findings for the a11y status labels vs the verbs each group dispatches."""
    text = code_only(raw)
    bad = []
    for group, sub_fn, table in STATUS_GROUPS:
        dispatched = dispatched_verbs(text, sub_fn)
        if dispatched is None or not dispatched:
            bad.append(f"{group}: the {sub_fn}() dispatch block is gone — cannot tell which verbs exist")
            continue
        # The status branch must render from the shared table, not from a second
        # hand-written verb list (that second list is what drifted).
        ref = re.search(
            r'"%s"\s*->\s*A11yStatusLabels\.labelFor\(\s*A11yStatusLabels\.(TAP|INPUT|SCROLL)\b' % group,
            text,
        )
        if ref is None:
            bad.append(f"{group}: statusForSubAction no longer renders through A11yStatusLabels.labelFor "
                       "(a second verb table is back — that is the shape that drifted)")
            continue
        named_table = ref.group(1)
        if named_table != table:
            bad.append(f"{group}: the status branch reads A11yStatusLabels.{named_table}, "
                       f"which is not the {table} table this group dispatches")
            continue
        named = table_keys(text, table)
        if named is None or not named:
            bad.append(f"{group}: A11yStatusLabels.{table} could not be read (probe lost its anchor)")
            continue
        for verb in sorted(dispatched - named):
            bad.append(f"{group}: dispatches '{verb}' but A11yStatusLabels.{table} has no label for it "
                       "(the overlay falls back to the generic text)")
        for verb in sorted(named - dispatched):
            bad.append(f"{group}: A11yStatusLabels.{table} names '{verb}' but nothing in {sub_fn}() dispatches it "
                       "(the capsule claims a verb the CLI cannot run)")
    return bad


# ── deep-audit-1030 ISSUE-07 ────────────────────────────────────────────────
# 4. ONE listing predicate. `A11yState.isServiceEnabled` used to carry its own
#    `split(':')`/`equals("<pkg>/<fq class>")` matcher while
#    `AccessibilityRecoveryManager.isRevokedIn` (the repair path) matched the
#    framework's abbreviated `pkg/.Class` form too. Same fact, two matchers, and
#    they drifted: the settings screen could report "disabled" for a service that
#    is bound and healthy. Both halves are asserted (local matcher gone; both
#    readers route through the shared predicate) — a revert of either one is red.
#
# 5. Prompt-slot identity. `continuation` is written by the waiter and read by
#    `respond` (UI thread); the teardown paths (timeout, cancellation) used to
#    clear it unconditionally, so a LATE teardown could dismiss the dialog a
#    newer waiter had just published — leaving that waiter parked for its whole
#    timeout. The token + lock shape is asserted here; see the gate's KDoc note
#    on why the concurrency itself is not reachable in this build.
# ────────────────────────────────────────────────────────────────────────────

A11Y_STATE = f"{PKG}/accessibility/A11yState.kt"
A11Y_RECOVERY = f"{PKG}/accessibility/AccessibilityRecoveryManager.kt"

# The removed local matcher, named so a revert reads as red.
LOCAL_MATCH_ANCHORS = ("split(':')", "equals(expected")
SHARED_JUDGE = "AccessibilityRecoveryManager.isRevokedIn("
MASTER_SWITCH = "A11yState.isMasterSwitchOn("
SLOT_DECL = "var continuation: Continuation<Decision>?"
SLOT_LOCK_DECL = "private val slotLock = Any()"
SLOT_RELEASE = "_pendingPrompt.value = null"
SLOT_RELEASE_WINDOW = 8
SLOT_STOPS = ("    suspend fun ", "    fun ", "    private fun ", "    internal fun ", "    @")


def scan_judge_single_source(state_raw, recovery_raw):
    """Findings for the listing predicate being single-sourced (empty = clean)."""
    bad = []
    state = code_only(state_raw)
    body = slice_body(state, "fun isServiceEnabled(")
    if body is None:
        bad.append("A11yState.isServiceEnabled() not found — the probe lost its subject")
    else:
        if SHARED_JUDGE not in body:
            bad.append("A11yState.isServiceEnabled no longer routes through "
                       "AccessibilityRecoveryManager.isRevokedIn — the listing predicate is "
                       "implemented a second time, which is exactly the shape that drifted")
        for anchor in LOCAL_MATCH_ANCHORS:
            if anchor in body:
                bad.append(f"A11yState.isServiceEnabled carries a local entry matcher ({anchor!r}) "
                           "— the fully-qualified-only spelling misses `pkg/.Class`, so a healthy "
                           "service reads as disabled while the repair path says otherwise")
        if "isMasterSwitchOn(" not in body:
            bad.append("A11yState.isServiceEnabled no longer consults the accessibility master "
                       "switch (a service bound with accessibility globally off receives nothing)")

    recovery = code_only(recovery_raw)
    body = slice_body(recovery, "fun isGrantRevoked(")
    if body is None:
        bad.append("AccessibilityRecoveryManager.isGrantRevoked() not found — the probe lost its subject")
    elif MASTER_SWITCH not in body:
        bad.append("isGrantRevoked does not short-circuit on A11yState.isMasterSwitchOn — with "
                   "accessibility globally off it reports a healthy grant while the service "
                   "receives no events (the drift this single-source rule exists to stop)")
    return bad


def release_guard_gaps(text):
    """Every release of the prompt slot must sit under a token comparison.

    Decomposition-independent on purpose: the family lesson (see
    `claim_release_guard`) is that a shared run-state flag loses its ownership
    guard the moment a SECOND release path is written without one, so the
    criterion is attached to the write itself, wherever it lives.
    """
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if SLOT_RELEASE in line]
    if not hits:
        return [f"the prompt slot has no release site (`{SLOT_RELEASE}` is gone) — the probe "
                "lost its subject"]
    bad = []
    for i in hits:
        window = "\n".join(lines[max(0, i - SLOT_RELEASE_WINDOW):i + 1])
        if "promptToken != " not in window:
            bad.append(f"the slot release (`{SLOT_RELEASE}`, line {i + 1}) is not "
                       "ownership-guarded — a late teardown can dismiss the prompt a NEWER "
                       "waiter is showing")
    return bad


def scan_slot_identity(recovery_raw):
    """Findings for the repair-prompt slot's identity + lock (empty = clean)."""
    text = code_only(recovery_raw)
    bad = []
    decl = next((l for l in text.split("\n") if SLOT_DECL in l), None)
    if decl is None:
        bad.append(f"the repair-prompt slot ({SLOT_DECL}) is gone — nothing arbitrates the dialog")
    elif "@Volatile" not in decl:
        bad.append("the prompt slot is no longer @Volatile — `respond` reads it on the UI thread "
                   "while the waiter writes it on the agent's")
    if SLOT_LOCK_DECL not in text:
        bad.append(f"the prompt slot has no lock ({SLOT_LOCK_DECL}) — publish / cleanup / respond "
                   "can interleave")

    await_body = slice_body(text, "private suspend fun awaitDecision(", SLOT_STOPS)
    if await_body is None:
        bad.append("awaitDecision() not found — the probe lost its subject")
    else:
        claim = "++promptToken" in await_body or "promptToken += 1" in await_body
        if not claim:
            bad.append("awaitDecision no longer claims a prompt token — with no owner id a late "
                       "teardown from an expired waiter can clear a NEWER waiter's prompt")
        elif "synchronized(slotLock) { ++promptToken }" not in await_body:
            bad.append("awaitDecision claims its token outside the lock")
        if claim and "val accepted = synchronized(slotLock) {" not in await_body:
            bad.append("awaitDecision publishes the slot without the lock — the publish can race "
                       "`respond` and a cleanup")
        if "invokeOnCancellation" not in await_body:
            bad.append("awaitDecision dropped invokeOnCancellation — a cancelled caller leaves the "
                       "dialog on screen and the slot owned by nobody")
        elif "clearSlotIfOwner(token)" not in await_body:
            bad.append("awaitDecision's teardown is not token-guarded (expected "
                       "`clearSlotIfOwner(token)`) — it must clear the slot only while it still "
                       "owns it")

    bad.extend(release_guard_gaps(text))

    respond_body = slice_body(text, "fun respond(", SLOT_STOPS)
    if respond_body is None:
        bad.append("respond() not found — the probe lost its subject")
    else:
        if "runCatching" not in respond_body:
            bad.append("respond resumes without runCatching — an already-cancelled continuation "
                       "throws straight through the UI callback")
        if "val taken = synchronized(slotLock) {" not in respond_body:
            bad.append("respond takes the slot outside the lock — it can interleave with a "
                       "cleanup and hand the decision to the wrong waiter")
    return bad


def selftest():
    """Negative controls: each section must reject its own mutated fixture and
    accept the clean one. A gate that cannot fail is not a gate."""
    clean_service = (
        "    fun captureScreenshot(displayId: Int = 0, timeoutMs: Long = 5_000): ShotResult {\n"
        "        val slot = LateValueSlot(timedOut) { late -> late.bitmap?.recycle() }\n"
        "        takeScreenshot(displayId, executor, object : TakeScreenshotCallback {\n"
        "            override fun onSuccess(screenshot: ScreenshotResult) {\n"
        "                if (!slot.isCallerWaiting()) return\n"
        "                val sw = bmp.copy(Bitmap.Config.ARGB_8888, false)\n"
        "                slot.publish(ShotResult(sw))\n"
        "            }\n"
        "        })\n"
        "        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) {\n"
        "            slot.exit(timedOut)\n"
        "            return timedOut\n"
        "        }\n"
        "        slot.exit(result)\n"
        "        return result\n"
        "    }\n"
        "\n"
        "    fun setNodeText(\n"
        "\n"
        "internal class LateValueSlot<T : Any>(private val fallback: T, private val onDiscard: (T) -> Unit) {\n"
        "    fun publish(value: T): Boolean {\n"
        "        val displaced = ref.getAndSet(value)\n"
        "        if (displaced == null) {\n"
        "            ref.compareAndSet(value, null)\n"
        "            onDiscard(value)\n"
        "            return false\n"
        "        }\n"
        "        return true\n"
        "    }\n"
        "    fun exit(taken: T?): T? {\n"
        "        val leftover = ref.getAndSet(null)\n"
        "        if (leftover != null && leftover !== taken) onDiscard(leftover)\n"
        "        return leftover\n"
        "    }\n"
        "}\n"
    )
    clean_handler = (
        "fun handle() {\n"
        "    val args = OffloadArgs(argv, booleanFlags = setOf(\"help\", \"contains\"))\n"
        "    if (args.hasFlag(\"help\")) return ok(args)\n"
        "    if (args.hasFlag(\"contains\")) return tapText(args)\n"
        "}\n"
        "\n"
        "    private fun tapText(args: OffloadArgs): NativeOffloadResult {\n"
        "        if (TapTextFlow.plan(n.isClickable, n.performAction(ACTION_CLICK)) == TapTextFlow.Stage.CLICKED) {\n"
        "            return ok(args, JSONObject())\n"
        "        }\n"
        "        val tapped = tapXYRaw(svc, r.centerX(), r.centerY(), args)\n"
        "        if (TapTextFlow.fallbackSucceeded(tapped.exitCode)) return tapped\n"
        "        return err(args, \"ACTION_FAILED\", \"no tap\")\n"
        "    }\n"
        "\n"
        "    private fun findByTextOrDesc() {}\n"
    )

    def _rows(entries):
        return "".join('        "%s" to "%s",\n' % (k, v) for k, v in entries)

    def _table(name, entries):
        return "    val %s: Map<String, String> = mapOf(\n%s    )\n\n" % (name, _rows(entries))

    def _dispatch(sub_fn, verbs, help_const, group):
        rows = "".join('            "%s" -> act(args)\n' % v for v in verbs)
        return (
            "    private fun %s(args: OffloadArgs): NativeOffloadResult {\n"
            "        return when (args.positional.getOrNull(1)) {\n"
            "            null   -> NativeOffloadResult(2, %s)\n"
            "%s"
            '            else   -> NativeOffloadResult(2, "$TOOL %s: unknown action\\n$%s")\n'
            "        }\n"
            "    }\n\n"
        ) % (sub_fn, help_const, rows, group, help_const)

    def status_fixture():
        """A miniature of the shipped shape: the three tables, the three dispatch
        blocks, and a statusForSubAction that renders from the tables."""
        return (
            "internal object A11yStatusLabels {\n"
            + _table("TAP", [("node", "a11y_cli: tap node %s"), ("xy", "a11y_cli: tap at (%s)"),
                             ("text", 'a11y_cli: tap \\"%s\\"'), ("id", "a11y_cli: tap #%s")])
            + _table("INPUT", [("text", "a11y_cli: input text"), ("clear", "a11y_cli: clearing input"),
                               ("key", "a11y_cli: input key %s")])
            + _table("SCROLL", [("node", "a11y_cli: scroll node %s"), ("xy", "a11y_cli: scroll at (%s)"),
                                ("to-text", 'a11y_cli: scrolling to \\"%s\\"')])
            + "}\n\n"
            + _dispatch("tapSub", ["node", "xy", "text", "id"], "TAP_HELP", "tap")
            + _dispatch("inputSub", ["text", "clear", "key"], "INPUT_HELP", "input")
            + _dispatch("scrollSub", ["node", "xy", "to-text"], "SCROLL_HELP", "scroll")
            + "    private fun statusForSubAction(sub: String, verb: String?, args: OffloadArgs): String {\n"
              "        val v = verb ?: \"\"\n"
              "        return when (sub) {\n"
              '            "tap" -> A11yStatusLabels.labelFor(A11yStatusLabels.TAP, v, null)\n'
              '                ?: "a11y_cli: tap"\n'
              '            "input" -> A11yStatusLabels.labelFor(A11yStatusLabels.INPUT, v, null)\n'
              '                ?: "a11y_cli: input"\n'
              '            "scroll" -> A11yStatusLabels.labelFor(A11yStatusLabels.SCROLL, v, null)\n'
              '                ?: "a11y_cli: scroll"\n'
              "        }\n"
              "    }\n"
        )

    clean_status = status_fixture()

    # ── deep-audit-1030 ISSUE-07: one listing predicate, one owned slot ──
    clean_judge_state = (
        "object A11yState {\n"
        "    fun isServiceEnabled(context: Context): Boolean {\n"
        "        if (!isMasterSwitchOn(context)) return false\n"
        "        val enabled = Settings.Secure.getString(\n"
        "            context.contentResolver,\n"
        "            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,\n"
        "        ) ?: return false\n"
        "        return !AccessibilityRecoveryManager.isRevokedIn(\n"
        "            enabled,\n"
        "            context.packageName,\n"
        "            MinisAccessibilityService::class.java.name,\n"
        "        )\n"
        "    }\n"
        "\n"
        "    fun isMasterSwitchOn(context: Context): Boolean {\n"
        "        return true\n"
        "    }\n"
        "}\n"
    )
    clean_judge_recovery = (
        "object AccessibilityRecoveryManager {\n"
        "    fun isGrantRevoked(context: Context): Boolean {\n"
        "        if (A11yState.isMasterSwitchOn(context)) {\n"
        "            return isRevokedIn(\n"
        "                enabledValue = raw,\n"
        "                pkg = context.packageName,\n"
        "                serviceClass = MinisAccessibilityService::class.java.name,\n"
        "            )\n"
        "        } else {\n"
        "            return true\n"
        "        }\n"
        "    }\n"
        "\n"
        "    fun isRevokedIn(enabledValue: String?, pkg: String, serviceClass: String): Boolean = true\n"
        "}\n"
    )
    clean_slot = (
        "object AccessibilityRecoveryManager {\n"
        "    private val slotLock = Any()\n"
        "\n"
        "    @Volatile private var continuation: Continuation<Decision>? = null\n"
        "\n"
        "    private var promptToken: Long = 0L\n"
        "\n"
        "    private suspend fun awaitDecision(prompt: RepairPrompt): Decision {\n"
        "        val token = synchronized(slotLock) { ++promptToken }\n"
        "        val decision = withTimeoutOrNull(PROMPT_TIMEOUT_MS) {\n"
        "            suspendCancellableCoroutine<Decision> { cont ->\n"
        "                val accepted = synchronized(slotLock) {\n"
        "                    if (promptToken != token) {\n"
        "                        false\n"
        "                    } else {\n"
        "                        continuation = cont\n"
        "                        true\n"
        "                    }\n"
        "                }\n"
        "                if (!accepted) {\n"
        "                    runCatching { cont.resume(Decision.CANCEL) }\n"
        "                    return@suspendCancellableCoroutine\n"
        "                }\n"
        "                cont.invokeOnCancellation { clearSlotIfOwner(token) }\n"
        "            }\n"
        "        }\n"
        "        if (decision == null) {\n"
        "            clearSlotIfOwner(token)\n"
        "        }\n"
        "        return decision ?: Decision.CANCEL\n"
        "    }\n"
        "\n"
        "    private fun clearSlotIfOwner(token: Long) {\n"
        "        synchronized(slotLock) { clearSlotLocked(token) }\n"
        "    }\n"
        "\n"
        "    private fun clearSlotLocked(token: Long): Continuation<Decision>? {\n"
        "        if (promptToken != token) return null\n"
        "        val taken = continuation\n"
        "        continuation = null\n"
        "        _pendingPrompt.value = null\n"
        "        return taken\n"
        "    }\n"
        "\n"
        "    fun respond(decision: Decision) {\n"
        "        val taken = synchronized(slotLock) {\n"
        "            val token = ++promptToken\n"
        "            clearSlotLocked(token)\n"
        "        }\n"
        "        runCatching { taken?.resume(decision) }\n"
        "    }\n"
        "}\n"
    )

    cases = [
        ("service/clean", clean_service),
        ("service/resultRef is back", clean_service.replace(
            'val slot = LateValueSlot(timedOut) { late -> late.bitmap?.recycle() }',
            'val resultRef = AtomicReference(ShotResult(null, "TIMEOUT", ""))').replace(
            "slot.publish(ShotResult(sw))", "resultRef.set(ShotResult(sw))")),
        ("service/guard after the copy", clean_service.replace(
            "                if (!slot.isCallerWaiting()) return\n                val sw",
            "                val sw")),
        ("service/timeout does not close the slot", clean_service.replace("            slot.exit(timedOut)\n", "")),
        ("service/publish keeps the refused value", clean_service.replace(
            "            ref.compareAndSet(value, null)\n", "")),
        ("service/exit stops handing over", clean_service.replace(
            "        if (leftover != null && leftover !== taken) onDiscard(leftover)\n", "")),
        ("handler/clean", clean_handler),
        ("handler/undeclared contains", clean_handler.replace('"help", "contains"', '"help"')),
        ("handler/tapText assumes ok", clean_handler.replace(
            "        if (TapTextFlow.fallbackSucceeded(tapped.exitCode)) return tapped\n"
            "        return err(args, \"ACTION_FAILED\", \"no tap\")\n",
            "        return tapped\n")),
        ("handler/discarded performAction", clean_handler.replace(
            "        if (TapTextFlow.plan(n.isClickable, n.performAction(ACTION_CLICK)) == TapTextFlow.Stage.CLICKED) {\n"
            "            return ok(args, JSONObject())\n"
            "        }\n",
            "        if (n.isClickable) n.performAction(ACTION_CLICK)\n")),
        # ── status labels vs dispatched verbs (deep-audit-1025 ISSUE-03) ──
        ("status/clean", clean_status),
        # A dispatched verb with no label: the overlay falls back to generic text.
        ("status/tap xy dispatches, table cannot name it", clean_status.replace(
            '        "xy" to "a11y_cli: tap at (%s)",\n', "")),
        ("status/input key dispatches, table cannot name it", clean_status.replace(
            '        "key" to "a11y_cli: input key %s",\n', "")),
        ("status/scroll to-text dispatches, table cannot name it", clean_status.replace(
            '        "to-text" to "a11y_cli: scrolling to \\"%s\\"",\n', "")),
        # The reverse direction: the capsule claims a verb the CLI cannot run —
        # exactly the shipped bug (`forward`/`set`/`by-text`).
        ("status/table names a verb nothing dispatches", clean_status.replace(
            '        "to-text" to "a11y_cli: scrolling to \\"%s\\"",\n',
            '        "to-text" to "a11y_cli: scrolling to \\"%s\\"",\n'
            '        "forward" to "a11y_cli: scroll forward",\n')),
        ("status/table reads the wrong group's table", clean_status.replace(
            "            \"tap\" -> A11yStatusLabels.labelFor(A11yStatusLabels.TAP, v, null)\n",
            "            \"tap\" -> A11yStatusLabels.labelFor(A11yStatusLabels.INPUT, v, null)\n")),
        ("status/second hand-written table is back", clean_status.replace(
            '            "tap" -> A11yStatusLabels.labelFor(A11yStatusLabels.TAP, v, null)\n',
            '            "tap" -> when (v) {\n'
            '                "by-text" -> "a11y_cli: tap \\"Login\\""\n'
            '                else -> "a11y_cli: tap"\n'
            "            }\n")),
        ("status/table literal lost", clean_status.replace(_table("SCROLL", [
            ("node", "a11y_cli: scroll node %s"), ("xy", "a11y_cli: scroll at (%s)"),
            ("to-text", 'a11y_cli: scrolling to \\"%s\\"')]), "")),
        # ── one listing predicate (deep-audit-1030 ISSUE-07 裂缝A) ──
        ("judge/clean", (clean_judge_state, clean_judge_recovery)),
        # The pre-fix shape: the screen keeps its own fully-qualified-only matcher.
        ("judge/state keeps its own entry matcher", (clean_judge_state.replace(
            "        return !AccessibilityRecoveryManager.isRevokedIn(\n",
            '        val expected = "${context.packageName}/${MinisAccessibilityService::class.java.name}"\n'
            "        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }\n"), clean_judge_recovery)),
        ("judge/state drops the master switch", (clean_judge_state.replace(
            "        if (!isMasterSwitchOn(context)) return false\n", ""), clean_judge_recovery)),
        ("judge/arm drops the master-switch short circuit", (clean_judge_state, clean_judge_recovery.replace(
            "if (A11yState.isMasterSwitchOn(context)) {", "if (true) {"))),
        # ── prompt-slot identity + lock (deep-audit-1030 ISSUE-07 裂缝B) ──
        ("slot/clean", clean_slot),
        ("slot/loses @Volatile", clean_slot.replace(
            "@Volatile private var continuation", "private var continuation")),
        ("slot/teardown is unconditional again", clean_slot.replace(
            "        if (promptToken != token) return null\n", "")),
        ("slot/respond resumes without runCatching", clean_slot.replace(
            "runCatching { taken?.resume(decision) }", "taken?.resume(decision)")),
        ("slot/never claims a token", clean_slot.replace(
            "val token = synchronized(slotLock) { ++promptToken }", "val token = 0L")),
        ("slot/drops invokeOnCancellation", clean_slot.replace(
            "                cont.invokeOnCancellation { clearSlotIfOwner(token) }\n", "")),
        ("slot/publishes without the lock", clean_slot.replace(
            "                val accepted = synchronized(slotLock) {", "                val accepted = run {")),
        ("slot/respond outside the lock", clean_slot.replace(
            "        val taken = synchronized(slotLock) {", "        val taken = run {")),
    ]

    print("a11y_handler_contract_check --selftest")
    failures = 0
    status_failures = 0
    judge_failures = 0
    slot_failures = 0
    for name, fixture in cases:
        expect_clean = name.endswith("/clean")
        if name.startswith("service/"):
            bad = scan_service(fixture)
        elif name.startswith("status/"):
            bad = scan_status_tables(fixture)
        elif name.startswith("judge/"):
            bad = scan_judge_single_source(*fixture)
        elif name.startswith("slot/"):
            bad = scan_slot_identity(fixture)
        else:
            bad = scan_handler(fixture)
        ok = (not bad) if expect_clean else bool(bad)
        print(f"  {'✅' if ok else '❌'} {name} → {'clean' if not bad else '; '.join(bad)[:90]}")
        if not ok:
            failures += 1
            if name.startswith("status/"):
                status_failures += 1
            elif name.startswith("judge/"):
                judge_failures += 1
            elif name.startswith("slot/"):
                slot_failures += 1
    print(f"  {'✅ selftest passed' if not failures else f'❌ selftest failures: {failures}'}")
    print(f"A11Y_STATUS_SELFTEST={'PASS' if status_failures == 0 else 'FAIL'}")
    print(f"A11Y_JUDGE_SINGLE_SOURCE_SELFTEST={'PASS' if judge_failures == 0 else 'FAIL'}")
    print(f"A11Y_SLOT_IDENTITY_SELFTEST={'PASS' if slot_failures == 0 else 'FAIL'}")
    return 1 if failures else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"Accessibility CLI contract check — {root}")
    total = 0
    for label, rel, scan in (
        ("MinisAccessibilityService (late screenshot reply)", SERVICE, scan_service),
        ("AccessibilityOffloadHandler (flags + tap outcome)", HANDLER, scan_handler),
        ("AccessibilityOffloadHandler (status verbs == dispatched verbs)", HANDLER, scan_status_tables),
    ):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print(f"❌ {rel}: missing")
            total += 1
            continue
        with open(path, encoding="utf-8") as f:
            bad = scan(f.read())
        if bad:
            print(f"❌ {label}")
            for b in bad:
                print(f"   - {b}")
            total += len(bad)
        else:
            print(f"✅ {label}")

    # Multi-file faces: the fact lives in one place, its readers in another.
    for label, rels, scan in (
        ("accessibility listing predicate (single source: A11yState ↔ recovery)",
         (A11Y_STATE, A11Y_RECOVERY), scan_judge_single_source),
        ("AccessibilityRecoveryManager (prompt slot: identity + lock)",
         (A11Y_RECOVERY,), scan_slot_identity),
    ):
        texts = []
        gap = False
        for rel in rels:
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                print(f"❌ {rel}: missing")
                total += 1
                gap = True
                break
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        if gap:
            continue
        bad = scan(*texts)
        if bad:
            print(f"❌ {label}")
            for b in bad:
                print(f"   - {b}")
            total += len(bad)
        else:
            print(f"✅ {label}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
