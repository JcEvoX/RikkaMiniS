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
    ]

    print("a11y_handler_contract_check --selftest")
    failures = 0
    for name, fixture in cases:
        expect_clean = name.endswith("/clean")
        bad = scan_service(fixture) if name.startswith("service/") else scan_handler(fixture)
        ok = (not bad) if expect_clean else bool(bad)
        print(f"  {'✅' if ok else '❌'} {name} → {'clean' if not bad else '; '.join(bad)[:90]}")
        if not ok:
            failures += 1
    print(f"  {'✅ selftest passed' if not failures else f'❌ selftest failures: {failures}'}")
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
