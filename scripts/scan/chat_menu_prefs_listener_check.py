#!/usr/bin/env python3
"""Guard: ChatMenuSettingsScreen's prefs-change listener must match the two
fixed top-bar/composer keys AND re-read the three order states.

Why this exists (da1021-D): the listener guarded on
`key.startsWith("chatMenu.")` ONLY, but the two fixed knobs it exists to
refresh are `topBar.inputHistory.visible` and `composer.modelPickerButton.visible`
(neither starts with `chatMenu.`), so external `minis-config` /
backup-restore writes never bumped the tick and the two switches kept
rendering the values read at first composition. On top of that, the three
order States (`menuOrder` / `footerOrder` / `attachOrder`) were never
re-read either: an external restore of `chatMenu.order` left the lists
stale and a subsequent in-screen drag wrote the stale order back.

Arms:
  1. the listener predicate matches BOTH fixed keys (via ChatMenuPrefs
     constants or the raw key strings);
  2. the listener body re-reads all three orders (resolveOrder( /
     settingsPinOrder( / resolveAttachOrder();
  3. the re-reads sit inside the `draggingEntry == null` guard (so an async
     listener notification can't interrupt an in-flight drag / write-back);
  4. the two key constants in ChatMenuPrefs are NOT `private const val`
     (the screen references them — a private revert forces a second string
     copy in the UI, the same-family recurrence shape).

Judgements are made on comment-stripped code (comment anchors rot); raw text
is also accepted for arm 1 so a string-literal form is not silently missed.
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ChatMenuSettingsScreen.kt"
PREFS_REL = "src/android/app/src/main/java/com/rikkaminis/app/config/ChatMenuPrefs.kt"

MARKER = "CHAT_MENU_PREFS_LISTENER_SELFTEST=PASS"

CONST_A = "TOP_BAR_INPUT_HISTORY_PREF"
CONST_B = "COMPOSER_MODEL_PICKER_BUTTON_PREF"
KEY_A = "topBar.inputHistory.visible"
KEY_B = "composer.modelPickerButton.visible"

ORDER_READS = ("resolveOrder(", "settingsPinOrder(", "resolveAttachOrder(")


def strip_comments_and_strings(text: str) -> str:
    """Blank out // comments, /* */ blocks and string literals (brace-safe)."""
    out = []
    i, n = 0, len(text)
    state = None
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state is None:
            if two == "//":
                state = "line"; out.append("  "); i += 2; continue
            if two == "/*":
                state = "block"; out.append("  "); i += 2; continue
            if c == '"':
                state = "str"; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = None
            i += 1; continue
        if state == "block":
            if two == "*/":
                state = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "str":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == '"':
                state = None; out.append(" "); i += 1; continue
            out.append(" "); i += 1; continue
    return "".join(out)


def listener_body(screen_text: str):
    """(stripped_body, raw_body) of the DisposableEffect block that registers
    the change listener. strip_comments_and_strings preserves character
    offsets, so both slices cover exactly the same region."""
    clean = strip_comments_and_strings(screen_text)
    for m in re.finditer(r"\bDisposableEffect\b[^\n{]*\{", clean):
        brace = clean.find("{", m.start())
        depth, i, n = 1, brace + 1, len(clean)
        while i < n and depth:
            if clean[i] == "{":
                depth += 1
            elif clean[i] == "}":
                depth -= 1
            i += 1
        if ".registerOnSharedPreferenceChangeListener" in clean[brace:i]:
            return clean[brace:i], screen_text[brace:i]
    return "", ""


def guarded_reads(body: str) -> bool:
    """All three order reads sit inside the `draggingEntry == null` guard."""
    first_read = min((body.find(r) for r in ORDER_READS if body.find(r) >= 0), default=-1)
    if first_read < 0:
        return False
    guard = body.find("draggingEntry == null")
    if guard < 0 or guard > first_read:
        return False
    # brace-match the guarded block and require every read inside it
    brace = body.find("{", body.find("draggingEntry == null"))
    if brace < 0:
        return False
    depth, i, n = 1, brace + 1, len(body)
    while i < n and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    inner = body[brace:i]
    return all(r in inner for r in ORDER_READS)


def check_arms(screen_text: str, prefs_text: str):
    """Return (problems, ok_summary). Four arms, judge on stripped code."""
    problems = []
    body, raw_body = listener_body(screen_text)
    if not body:
        return ["could not locate the DisposableEffect listener block in "
                + SCREEN_REL + " -- the registration call may have moved"], []

    # arm 1: predicate matches both fixed keys (constant name in stripped
    # code, or raw key string in case a literal form is used) -- both judged
    # INSIDE the effect region only, so a KDoc mention elsewhere never fakes a pass
    a_ok = (CONST_A in body or KEY_A in raw_body)
    b_ok = (CONST_B in body or KEY_B in raw_body)
    if not (a_ok and b_ok):
        missing = []
        if not a_ok:
            missing.append(KEY_A)
        if not b_ok:
            missing.append(KEY_B)
        problems.append(
            "arm1: listener predicate does not match the fixed key(s) "
            + ", ".join(missing)
            + " -- the `chatMenu.` prefix alone never fires for external writes"
        )

    # arm 2: all three order re-reads present in the listener body
    missing = [r for r in ORDER_READS if r not in body]
    if missing:
        problems.append(
            "arm2: listener body does not re-read the order state(s): "
            + ", ".join(missing)
            + " -- an external restore of chatMenu.*Order leaves the lists stale"
        )

    # arm 3: re-reads inside the draggingEntry == null guard
    if not guarded_reads(body):
        problems.append(
            "arm3: the order re-reads are not inside the `draggingEntry == null` "
            "guard -- an async listener notification can interrupt an in-flight "
            "drag or overwrite the commit* write-back"
        )

    # arm 4: both key constants non-private in ChatMenuPrefs
    clean_prefs = strip_comments_and_strings(prefs_text)
    for name in (CONST_A, CONST_B):
        m = re.search(r"(private\s+)?const\s+val\s+" + name + r"\b", clean_prefs)
        if not m:
            problems.append(
                "arm4: ChatMenuPrefs." + name + " declaration not found -- "
                "the constant may have been renamed; re-point this guard"
            )
        elif m.group(1):
            problems.append(
                "arm4: ChatMenuPrefs." + name + " is `private const val` -- "
                "the screen cannot reference it and will drift to a second "
                "string copy (same-family recurrence shape)"
            )

    return problems, []


def run_selftest() -> bool:
    """Compliant fixture green + 4 violation fixtures red."""
    COMPLIANT_SCREEN = '''
@Composable
fun Screen() {
    DisposableEffect(context) {
        val prefs = context.applicationContext
            .getSharedPreferences(P, MODE_PRIVATE)
        val listener =
            OnSharedPreferenceChangeListener { _, key ->
                if (key != null && (
                        key.startsWith("chatMenu.") ||
                            key == ChatMenuPrefs.TOP_BAR_INPUT_HISTORY_PREF ||
                            key == ChatMenuPrefs.COMPOSER_MODEL_PICKER_BUTTON_PREF
                    )
                ) {
                    prefsTick++
                    if (draggingEntry == null) {
                        menuOrder = ChatMenuPrefs.resolveOrder(prefs)
                            .filter { ChatActionCatalog.spec(it)?.defaultMenuVisible != false }
                        footerOrder = ChatMenuPrefs.settingsPinOrder(prefs)
                        attachOrder = ChatMenuPrefs.resolveAttachOrder(prefs)
                    }
                }
            }
        prefs.registerOnSharedPreferenceChangeListener(listener)
        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }
    }
}
'''
    COMPLIANT_PREFS = '''
object ChatMenuPrefs {
    const val TOP_BAR_INPUT_HISTORY = "topBar.inputHistory"
    internal const val TOP_BAR_INPUT_HISTORY_PREF = "topBar.inputHistory.visible"
    const val COMPOSER_MODEL_PICKER_BUTTON = "composer.modelPickerButton"
    internal const val COMPOSER_MODEL_PICKER_BUTTON_PREF = "composer.modelPickerButton.visible"
}
'''
    M1_SCREEN = COMPLIANT_SCREEN.replace(
        'key == ChatMenuPrefs.TOP_BAR_INPUT_HISTORY_PREF ||\n'
        '                            key == ChatMenuPrefs.COMPOSER_MODEL_PICKER_BUTTON_PREF',
        'false')
    M2_SCREEN = COMPLIANT_SCREEN.replace(
        '''menuOrder = ChatMenuPrefs.resolveOrder(prefs)
                            .filter { ChatActionCatalog.spec(it)?.defaultMenuVisible != false }
                        footerOrder = ChatMenuPrefs.settingsPinOrder(prefs)
                        attachOrder = ChatMenuPrefs.resolveAttachOrder(prefs)''',
        'prefsTick++')
    M3_SCREEN = COMPLIANT_SCREEN.replace("if (draggingEntry == null) {", "if (true) {")
    M4_PREFS = COMPLIANT_PREFS.replace("internal const val", "private const val")

    cases = [
        ("compliant", COMPLIANT_SCREEN, COMPLIANT_PREFS, 0),
        ("M1 predicate missing fixed keys", M1_SCREEN, COMPLIANT_PREFS, 1),
        ("M2 no order re-reads", M2_SCREEN, COMPLIANT_PREFS, 1),
        ("M3 no draggingEntry guard", M3_SCREEN, COMPLIANT_PREFS, 1),
        ("M4 constants private again", COMPLIANT_SCREEN, M4_PREFS, 1),
    ]
    ok = True
    for name, screen, prefs, want_red in cases:
        problems, _ = check_arms(screen, prefs)
        red = len(problems) > 0
        status = "RED" if red else "GREEN"
        expected = "RED" if want_red else "GREEN"
        mark = "ok" if red == bool(want_red) else "FAIL"
        if mark == "FAIL":
            ok = False
        print(f"  selftest {name}: {status} (expect {expected}) {mark}")
        for p in problems:
            print(f"      - {p}")

    # shape edge: no listener at all -> explicit red, not a silent pass
    problems, _ = check_arms("@Composable fun S() {}", COMPLIANT_PREFS)
    if not problems:
        print("  selftest no-listener: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest no-listener: RED (expect RED) ok")
    return ok


def main() -> int:
    if "--selftest" in sys.argv:
        print("chat_menu_prefs_listener_check selftest:")
        if not run_selftest():
            print("FAIL: selftest")
            return 1
        print(MARKER)
        return 0

    screen_path = ROOT / SCREEN_REL
    prefs_path = ROOT / PREFS_REL
    if not screen_path.exists():
        print(f"FAIL: missing {SCREEN_REL}")
        return 1
    if not prefs_path.exists():
        print(f"FAIL: missing {PREFS_REL}")
        return 1
    problems, _ = check_arms(screen_path.read_text(encoding="utf-8", errors="replace"),
                             prefs_path.read_text(encoding="utf-8", errors="replace"))
    if problems:
        print("FAIL: chat menu settings listener gaps")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: chat menu prefs listener matches both fixed keys, "
          "re-reads all three orders inside the draggingEntry guard, "
          "and both key constants are non-private (2 sites checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
