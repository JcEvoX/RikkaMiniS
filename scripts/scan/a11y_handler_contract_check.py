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
    ]

    print("a11y_handler_contract_check --selftest")
    failures = 0
    status_failures = 0
    for name, fixture in cases:
        expect_clean = name.endswith("/clean")
        if name.startswith("service/"):
            bad = scan_service(fixture)
        elif name.startswith("status/"):
            bad = scan_status_tables(fixture)
        else:
            bad = scan_handler(fixture)
        ok = (not bad) if expect_clean else bool(bad)
        print(f"  {'✅' if ok else '❌'} {name} → {'clean' if not bad else '; '.join(bad)[:90]}")
        if not ok:
            failures += 1
            if name.startswith("status/"):
                status_failures += 1
    print(f"  {'✅ selftest passed' if not failures else f'❌ selftest failures: {failures}'}")
    print(f"A11Y_STATUS_SELFTEST={'PASS' if status_failures == 0 else 'FAIL'}")
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
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
