#!/usr/bin/env python3
"""deep-audit-1046 S-06 (P1) — floating-overlay show/hide intent latch gate.

Pins the fix from branch fix/dv26-c6-overlay-show-hide-latch.

The defect: `ToolOverlayController.show()` publishes the capsule from a
POSTED Runnable, so `isShown` — flipped only once that Runnable actually
reaches `attach()` — is false for at least one main-thread queue hop. All
three hide sites used to be gated on it (`if (isShown) hide()`), so any
state flip landing inside that hop ("app came to the foreground" / "camera
suppress" / "toggle off" / "task finished") was silently dropped, and the
still-queued attach then painted the capsule over the chat or the camera
preview with no later state frame to clear it.

The fix is an INTENT LATCH that is written synchronously at both entry
points and re-read inside the posted show block. This gate pins four
independent shapes, all DERIVED (never anchored to a member name):

  C1  `show()` entry assigns <intent> = true BEFORE queueing the post, and
      `hide()` entry assigns <intent> = false BEFORE queueing the post.
      (the intent field itself is derived: the Boolean field assigned true
      in show() and false in hide())
  C2  the Runnable show() posts re-checks that intent field and early-returns
      BEFORE it can call attach() — the "guard moved after attach" shape is
      red because by then the view is already on screen.
  C3  no hide() call site is gated on the REAL attach-state field (derived:
      the Boolean field whose `= true` assignment lives inside attach()),
      because that field lags the intent by a queue hop.
  C4  the intent field and the real attach-state field are two DISTINCT
      fields — collapsing them back into one is the original bug.

Deliberately NOT pinned (so a rename or a re-ordering of unrelated members
does not turn this red): member names, line numbers, the shape of the guard
call (`shouldRunShowPost(desired)` today), the `lingerJob` / completion
linger semantics in AgentForegroundService.

Usage: python3 overlay_show_hide_latch_check.py [--root ROOT] [ROOT]
       python3 overlay_show_hide_latch_check.py --selftest
Exit:  0 = gate green, 1 = violations / selftest fail, 2 = usage error.
"""
import re
import sys
import tempfile
from pathlib import Path

CONTROLLER = "src/android/app/src/main/java/com/rikkaminis/app/service/ToolOverlayController.kt"
SERVICE = "src/android/app/src/main/java/com/rikkaminis/app/service/AgentForegroundService.kt"

SELFTEST_MARKER = "OVERLAY_SHOW_HIDE_LATCH_SELFTEST=PASS"

CRIT_C1 = "C1 show()/hide() publish the intent latch synchronously, before queueing the post"
CRIT_C2 = "C2 the posted show Runnable re-checks the intent latch before attach()"
CRIT_C3 = "C3 no hide() call site is gated on the lagging real attach-state field"
CRIT_C4 = "C4 intent latch and real attach-state are two distinct fields"


# --------------------------------------------------------------------------
# source preparation: comments AND string bodies blanked, newlines preserved
# --------------------------------------------------------------------------
def prepare(src: str) -> str:
    """Blank out comments and string bodies; every newline is kept, so byte
    offsets and line numbers still map 1:1 onto the original file."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
        elif c == '"':
            if src.startswith('"""', i):
                j = src.find('"""', i + 3)
                end = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n and src[j] != '"' and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                end = min(j + 1, n)
            for k in range(i, end):
                out.append("\n" if src[k] == "\n" else " ")
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def block_at(text: str, open_idx: int) -> str:
    """Braced block starting at the '{' at open_idx (text must be prepared)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return text[open_idx:]


def find_fun(text: str, pattern: str) -> tuple[str, int] | None:
    m = re.search(pattern, text)
    if not m:
        return None
    brace = text.find("{", m.end() - 1)
    if brace < 0:
        return None
    return block_at(text, brace), m.start()


def paren_cond(text: str, open_idx: int) -> tuple[str, int]:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i
    return text[open_idx + 1:], len(text) - 1


def line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


def assign_positions(body: str, name: str, value: str) -> list[int]:
    pat = re.compile(r"(?<![.\w])" + re.escape(name) + r"\s*=\s*" + value + r"(?![\w.])")
    return [m.start() for m in pat.finditer(body)]


def bool_fields(class_body: str) -> list[str]:
    return re.findall(r"\bvar\s+(\w+)\s*:\s*Boolean\b", class_body)


def literal_decides(cond: str) -> bool:
    """True when a literal operand decides [cond]'s value.

    [wide-1024 C2] `if (!shouldRunShowPost(latch) && false) return@post` still
    *mentions* the latch and still *contains* an early return, so every
    substring criterion the old arm used stays satisfied while the guard is
    dead. A conjunction that contains a bare `false` (or a disjunction with a
    bare `true`) is decided by that literal, not by the latch."""
    for part in re.split(r"&&|\|\|", cond):
        t = part.strip().strip("()").strip()
        if t in ("true", "false", "!true", "!false"):
            return True
    return False


def paren_cond_at(text: str, close_idx: int) -> tuple[str | None, int]:
    """Condition text of the `(…)` group whose ')' sits at [close_idx]."""
    depth = 0
    i = close_idx
    while i >= 0:
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return text[i + 1:close_idx], i
        i -= 1
    return None, -1


def enclosing_conds(text: str, pos: int) -> list[str]:
    """Header conditions of every brace block that textually encloses [pos].

    [wide-1024 C3] The old scan only looked at the hide() call's own line and
    the line above, so the canonical Kotlin block form

        if (controller.isShown) {
            controller.hide()
        }

    was invisible: the brace-brace form failed `prev.rstrip().endswith(")")`.
    Conditions are read from the block HEADERS (if / while / when), brace
    matched backwards, so the spelling of the body does not matter."""
    out: list[str] = []
    depth = 0
    i = pos - 1
    while i >= 0:
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                j = i - 1
                while j >= 0 and text[j] in " \t\n":
                    j -= 1
                if j >= 0 and text[j] == ")":
                    cond, open_at = paren_cond_at(text, j)
                    if cond is not None:
                        head = text[max(0, open_at - 12):open_at]
                        if re.search(r"\b(if|while|when)\s*$", head):
                            out.append(cond)
            else:
                depth -= 1
        i -= 1
    return out


def hide_gates(src: str) -> list[tuple[int, list[str]]]:
    """(line, governing conditions) for every hide() CALL site."""
    out: list[tuple[int, list[str]]] = []
    for m in re.finditer(r"\bhide\s*\(\s*\)", src):
        pre = src[max(0, m.start() - 30):m.start()]
        if re.search(r"\bfun\s*$", pre):
            continue  # the declaration itself
        line_start = src.rfind("\n", 0, m.start()) + 1
        prefix = src[line_start:m.start()]
        conds: list[str] = []
        for im in re.finditer(r"\bif\s*\(", prefix):
            c, _ = paren_cond(prefix, im.end() - 1)
            conds.append(c)
        if re.search(r"takeIf|takeUnless", prefix):
            conds.append(prefix)
        conds.extend(enclosing_conds(src, m.start()))
        out.append((line_of(src, m.start()), conds))
    return out


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def analyse(ctrl: str, svc: str) -> list[tuple[str, bool, str]]:
    """ctrl / svc = prepared sources. Returns (criterion, ok, detail)."""
    out: list[tuple[str, bool, str]] = []

    cls = block_at(ctrl, ctrl.find("{", ctrl.find("class ToolOverlayController")))
    fields = bool_fields(cls)
    show_f = find_fun(cls, r"\bfun\s+show\s*\(")
    hide_f = find_fun(cls, r"\bfun\s+hide\s*\(\s*\)\s*\{")
    attach_f = find_fun(cls, r"\bfun\s+attach\s*\(\s*\)")

    # ---- derive the two roles -------------------------------------------
    intent = None
    intent_show_at = intent_hide_at = -1
    if show_f and hide_f:
        for f in fields:
            s = assign_positions(show_f[0], f, "true")
            h = assign_positions(hide_f[0], f, "false")
            if s and h:
                intent, intent_show_at, intent_hide_at = f, s[0], h[0]
                break

    real = None
    if attach_f:
        for f in fields:
            if assign_positions(attach_f[0], f, "true"):
                real = f
                break

    # ---- C1: intent written at BOTH entries, before the post -----------
    if not show_f or not hide_f:
        out.append((CRIT_C1, False, "ToolOverlayController.show()/hide() not found"))
    elif intent is None:
        out.append((
            CRIT_C1, False,
            "no Boolean field is assigned true in show() and false in hide() "
            "— the intent is not being published",
        ))
    else:
        bad = []
        for label, body, pos in (("show()", show_f[0], intent_show_at),
                                 ("hide()", hide_f[0], intent_hide_at)):
            post = re.search(r"\.post\s*[({]", body)
            if post and pos > post.start():
                bad.append(f"{label} writes {intent} only AFTER queueing the post")
        # [wide-1024 C1] The entry must PUBLISH the intent and keep it: a
        # second write of the opposite value (hide() re-latching `= true`, or
        # show() re-latching `= false`) silently undoes the publication while
        # every position/substring criterion above stays satisfied.
        if assign_positions(show_f[0], intent, "false"):
            bad.append(
                f"show() re-latches {intent} = false after publishing it — the intent "
                "is dropped again before the post runs"
            )
        if assign_positions(hide_f[0], intent, "true"):
            bad.append(
                f"hide() re-latches {intent} = true after publishing it — the hide "
                "intent is overwritten and a queued attach survives"
            )
        if bad:
            out.append((CRIT_C1, False, "; ".join(bad)))
        else:
            out.append((CRIT_C1, True, f"intent field = {intent}"))

    # ---- C2: posted show Runnable re-checks it before attach() ---------
    if not show_f:
        out.append((CRIT_C2, False, "show() not found"))
    elif intent is None:
        out.append((CRIT_C2, False, "no intent field derived (see C1)"))
    else:
        body = show_f[0]
        post = re.search(r"\.post\s*\{", body)
        if not post:
            out.append((CRIT_C2, False, "show() posts no Runnable (shape changed)"))
        else:
            runnable = block_at(body, post.end() - 1)
            guard = -1
            guard_cond, guard_lit = "", False
            for m in re.finditer(r"\bif\s*\(", runnable):
                cond, close = paren_cond(runnable, m.end() - 1)
                if not re.search(r"(?<![\w])" + re.escape(intent) + r"(?!\w)", cond):
                    continue
                tail = runnable[close + 1: close + 120].lstrip()
                if tail.startswith("{") or tail.startswith("return"):
                    guard, guard_cond = m.start(), cond
                    guard_lit = literal_decides(cond)
                    break
            attach_at = runnable.find("attach(")
            if guard >= 0 and guard_lit:
                out.append((
                    CRIT_C2, False,
                    f"the posted show Runnable's {intent} guard is decided by a literal "
                    f"(`{guard_cond.strip()}`) — the early return can never fire, so a "
                    "hide inside the queue hop cannot cancel the attach",
                ))
            elif guard < 0:
                out.append((
                    CRIT_C2, False,
                    f"the posted show Runnable never checks {intent} — a hide inside "
                    "the queue hop cannot cancel the attach",
                ))
            elif attach_at >= 0 and guard > attach_at:
                out.append((
                    CRIT_C2, False,
                    f"the {intent} guard sits AFTER attach() in the posted Runnable "
                    "(the view is already on screen)",
                ))
            else:
                out.append((CRIT_C2, True, f"guard on {intent} precedes attach()"))

    # ---- C3: no hide() call site gated on the real attach-state --------
    viol = []
    if real is None:
        viol.append("could not derive the real attach-state field (no `= true` inside attach())")
    else:
        for path, src in ((CONTROLLER, ctrl), (SERVICE, svc)):
            for line_no, conds in hide_gates(src):
                hits = [
                    c for c in conds
                    if re.search(r"(?<![\w])" + re.escape(real) + r"(?!\w)", c)
                ]
                if hits:
                    viol.append(
                        f"{path.rsplit('/', 1)[-1]}:{line_no} "
                        f"hide() gated on `{hits[0].strip()}` — `{real}` lags the intent "
                        "by a queue hop, so this drops the hide"
                    )
        if viol:
            out.append((CRIT_C3, False, "; ".join(viol)))
        else:
            out.append((CRIT_C3, True, f"no hide() gated on {real}"))

    # ---- C4: intent and real state must be distinct --------------------
    if intent is None or real is None:
        out.append((
            CRIT_C4, False,
            f"both fields must exist and differ (intent={intent!r}, real={real!r})",
        ))
    elif intent == real:
        out.append((
            CRIT_C4, False,
            f"`{real}` doubles as the intent latch — an async flag cannot also be "
            "the synchronous intent (this is the original defect)",
        ))
    else:
        out.append((CRIT_C4, True, f"intent={intent} / real={real}"))

    return out


def scan(root: Path) -> tuple[list[str], list[str]]:
    """Returns (violations, summary_lines)."""
    ctrl_p, svc_p = root / CONTROLLER, root / SERVICE
    missing = [str(p) for p in (ctrl_p, svc_p) if not p.is_file()]
    if missing:
        return [f"missing source file(s): {', '.join(missing)}"], []
    results = analyse(
        prepare(ctrl_p.read_text(encoding="utf-8")),
        prepare(svc_p.read_text(encoding="utf-8")),
    )
    violations = []
    summary = []
    for crit, ok, detail in results:
        if ok:
            summary.append(f"  OK   {crit} [{detail}]")
        else:
            violations.append(f"{crit}: {detail}")
    return violations, summary


# --------------------------------------------------------------------------
# selftest — every arm is a byte-level mutation of the REAL tree
# --------------------------------------------------------------------------
def _mutate(src: str, old: str, new: str, tag: str) -> str:
    if src.count(old) != 1:
        raise SystemExit(
            f"[selftest {tag}] anchor must appear exactly once, found {src.count(old)}: "
            f"{old[:70]!r} — the gate's arms drifted from production; fix the gate"
        )
    return src.replace(old, new)


SHOW_POST_TAIL = """        mainHandler.post {
            if (!shouldRunShowPost(desiredShown)) return@post
            try {
"""
SHOW_LATCH = "        desiredShown = true\n" + SHOW_POST_TAIL
HIDE_HEAD = "        desiredShown = false\n"
SVC_GATE1 = "            controller.hide()\n            return\n"
SVC_IDLE = "        controller.hide()\n    }\n"
CTRL_NOPERM = "            hide()\n            return\n"


def selftest() -> int:
    root = Path(__file__).resolve().parents[2]
    ctrl = (root / CONTROLLER).read_text(encoding="utf-8")
    svc = (root / SERVICE).read_text(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="ovl-latch-"))

    def arm(name: str, ctrl_src: str, svc_src: str, expect: list[str]) -> bool:
        """expect = criteria that must be red; every other criterion must stay
        green (so "exactly one arm, one criterion" is enforced, not narrated)."""
        d = tmp / name
        for rel, src in ((CONTROLLER, ctrl_src), (SERVICE, svc_src)):
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        viol, _ = scan(d)
        reds = sorted({v.split(":")[0].split(" ")[0] for v in viol})
        ok = reds == sorted(expect)
        print(f"[selftest {name}] expect red {expect} -> got {reds}: {'OK' if ok else 'FAIL'}")
        for v in viol:
            print(f"    {v}")
        return ok

    good = True

    # arm 0: the real tree is green
    viol, summary = scan(root)
    if viol:
        print(f"[selftest ok-arm] EXPECTED green, got: {viol!r}")
        good = False
    else:
        print("[selftest ok-arm] real tree parses clean: OK")
    for line in summary:
        print("    " + line.strip())

    # arm 1: intent published only AFTER the post is queued, in show()
    late_show = _mutate(ctrl, SHOW_LATCH, SHOW_POST_TAIL, "c1")
    # re-add the latch as the last statement of show() instead of its entry
    late_show = _mutate(
        late_show,
        "                Log.w(TAG, \"show failed: ${e.message}\", e)\n"
        "            }\n        }\n    }\n\n    fun hide()",
        "                Log.w(TAG, \"show failed: ${e.message}\", e)\n"
        "            }\n        }\n        desiredShown = true\n    }\n\n    fun hide()",
        "c1b",
    )
    good &= arm("c1-latch-after-post", late_show, svc, ["C1"])

    # arm 2: hide() clears the latch only inside the posted body
    moved = _mutate(ctrl, HIDE_HEAD, "", "c2").replace(
        "        mainHandler.post {\n            val v = view ?: return@post",
        "        mainHandler.post {\n            desiredShown = false\n            val v = view ?: return@post",
        1,
    )
    good &= arm("c1-hide-late", moved, svc, ["C1"])

    # arm 3: the posted show Runnable drops its intent guard
    good &= arm("c2-no-guard", _mutate(ctrl, SHOW_POST_TAIL,
                                       "        mainHandler.post {\n            try {\n", "c3"), svc, ["C2"])

    # arm 4: guard moved behind attach()
    after = _mutate(
        ctrl,
        SHOW_POST_TAIL + "                pendingSessionId = targetSessionId\n"
        "                if (view == null) {\n"
        "                    Log.d(TAG, \"show() attach() — view was null\")\n"
        "                    attach()\n"
        "                }\n",
        "        mainHandler.post {\n            try {\n"
        "                pendingSessionId = targetSessionId\n"
        "                if (view == null) {\n"
        "                    Log.d(TAG, \"show() attach() — view was null\")\n"
        "                    attach()\n"
        "                }\n"
        "                if (!shouldRunShowPost(desiredShown)) return@post\n",
        "c4",
    )
    good &= arm("c2-guard-after-attach", after, svc, ["C2"])

    # arm 5/6/7: each of the three call sites re-gated on the real state
    good &= arm("c3-controller-noperm", _mutate(ctrl, CTRL_NOPERM,
                                                "            if (isShown) hide()\n            return\n", "c5"),
                svc, ["C3"])
    good &= arm("c3-service-gate1", ctrl, _mutate(
        svc, SVC_GATE1, "            if (controller.isShown) controller.hide()\n            return\n", "c6"), ["C3"])
    good &= arm("c3-service-idle", ctrl, _mutate(
        svc, SVC_IDLE, "        if (controller.isShown) controller.hide()\n    }\n", "c7"), ["C3"])

    # arm 8: collapse the two fields into one (the original defect shape)
    merged = _mutate(ctrl, "    @Volatile\n    private var desiredShown: Boolean = false\n", "", "c8")
    merged = merged.replace("desiredShown", "isShown")
    good &= arm("c4-merged-fields", merged, svc, ["C4"])

    # ── wide-1024 witness arms (each was green before this branch) ──────────
    # arm 10: hide() publishes `false` then immediately re-latches `true` —
    #         position/substring criteria stay satisfied, the intent is gone.
    good &= arm("c1-hide-relatch-true", _mutate(
        ctrl, HIDE_HEAD, "        desiredShown = false\n        desiredShown = true\n", "c9"),
        svc, ["C1"])

    # arm 11: the posted guard mentions the latch and still returns early, but
    #         a literal `&& false` decides it.
    good &= arm("c2-guard-literal-false", _mutate(
        ctrl,
        "            if (!shouldRunShowPost(desiredShown)) return@post\n",
        "            if (!shouldRunShowPost(desiredShown) && false) return@post\n", "c10"),
        svc, ["C2"])

    # arm 12: the canonical Kotlin BLOCK form of the gated hide — the old
    #         line-prefix scan could not see `) {` + a body line.
    good &= arm("c3-service-braced-gate", ctrl, _mutate(
        svc, SVC_GATE1,
        "            if (controller.isShown) {\n"
        "                controller.hide()\n"
        "            }\n            return\n", "c11"), ["C3"])

    # arm 9: missing file must be red, never silently green
    d = tmp / "missing"
    p = d / CONTROLLER
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ctrl, encoding="utf-8")
    viol, _ = scan(d)
    ok = bool(viol) and "missing" in viol[0]
    print(f"[selftest missing-file] expect missing-file violation -> {'OK' if ok else 'FAIL'}")
    good &= ok

    if good:
        print(SELFTEST_MARKER)
        return 0
    print("OVERLAY_SHOW_HIDE_LATCH_SELFTEST=FAIL")
    return 1


def main(argv: list[str]) -> int:
    root = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--selftest":
            return selftest()
        if a == "--root":
            i += 1
            if i >= len(argv):
                print("usage: --root needs a value", file=sys.stderr)
                return 2
            root = Path(argv[i])
        elif not a.startswith("--"):
            root = Path(a)
        i += 1
    if root is None:
        root = Path.cwd()
    if not root.is_dir():
        print(f"repo root not found: {root}", file=sys.stderr)
        return 2

    violations, _ = scan(root)
    if violations:
        print("[overlay-show-hide-latch] VIOLATIONS FOUND:")
        for v in violations:
            print(f"  FAIL: {v}")
        print(
            "Fix: publish the overlay INTENT at the show()/hide() entries (before the "
            "post), re-check it inside the posted show Runnable before attach(), keep "
            "hide() ungated (it is idempotent), and never let one field be both the "
            "synchronous intent and the asynchronous attach state."
        )
        return 1
    print(
        "[overlay-show-hide-latch] OK: 4 criteria clean — show/hide publish an intent "
        "latch synchronously, the posted attach re-checks it, all hide() sites are "
        "ungated, and intent != attach state"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
