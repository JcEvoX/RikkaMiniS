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

[da1028-B / ISSUE-04] Five more arms for the two stale-write screens (only
judged when the caller supplies their sources; the legacy two-file fixtures
leave them out):
  5. SoulStore declares `writeVersion` and `save()` bumps it — Settings → Soul
     keys its reload on it, so without the bump an external write leaves the
     editors stale and the next Save rolls that write back;
  6. SoulStore's `refreshCache()` bumps it too — that is the documented entry
     point for "SOUL.md was rewritten outside save()" (the chat memory sheet
     calls it right after its raw saveFile write);
  7. SoulSettingsScreen loads in a `LaunchedEffect` keyed on `writeVersion`
     and that effect re-reads the file (a `LaunchedEffect(Unit)` load is the
     pre-fix shape this arm exists to reject);
  8. LogManagementScreen registers an OnSharedPreferenceChangeListener for
     `logs.enabled` through AppLogger's constants (no re-typed literal) and
     unregisters it on dispose — the unregister half is required, not optional;
  9. anchors missing (store member / effect / listener block) -> red, never a
     silent skip.

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
# [da1028-B / ISSUE-04] stale-write surfaces
SOUL_STORE_REL = "src/android/app/src/main/java/com/rikkaminis/app/agent/SoulStore.kt"
SOUL_SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SoulSettingsScreen.kt"
LOG_SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/LogManagementScreen.kt"

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


def brace_body(clean: str, header_re: str) -> str:
    """Brace-matched body of the first member whose header matches header_re.
    Empty string when the header is absent (callers fail closed on "")."""
    m = re.search(header_re, clean)
    if not m:
        return ""
    brace = clean.find("{", m.end())
    if brace < 0:
        return ""
    depth, i, n = 1, brace + 1, len(clean)
    while i < n and depth:
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
        i += 1
    return clean[brace:i]


def check_stale_write_arms(soul_store_text: str, soul_screen_text: str,
                           log_screen_text: str):
    """[da1028-B / ISSUE-04] arms 5-8 (see module docstring). Only called when
    the caller supplied the three sources."""
    problems = []

    # ---- arm 5/6: the store's write counter ------------------------------
    store = strip_comments_and_strings(soul_store_text)
    if not re.search(r"\bval\s+writeVersion\b", store) or "_writeVersion" not in store:
        problems.append(
            "arm5: SoulStore exposes no writeVersion StateFlow -- outside writers "
            "(minis-config / restore / the chat memory sheet) have no way to tell "
            "Settings → Soul that the file changed"
        )
    else:
        save_body = brace_body(store, r"\bfun\s+save\s*\(\s*context\s*:")
        if not save_body:
            problems.append(
                "arm5: SoulStore.save(…) body not found -- re-point this guard"
            )
        elif not re.search(r"_writeVersion\.value\s*(\+=\s*1|\+\+|=\s*[^=])", save_body):
            problems.append(
                "arm5: SoulStore.save(…) no longer bumps _writeVersion -- every "
                "external write becomes invisible to the Soul editors, so the "
                "next Save silently rolls it back"
            )
        refresh_body = brace_body(store, r"\bfun\s+refreshCache\s*\(")
        if not refresh_body:
            problems.append(
                "arm6: SoulStore.refreshCache(…) body not found -- re-point this guard"
            )
        elif not re.search(r"_writeVersion\.value\s*(\+=\s*1|\+\+|=\s*[^=])", refresh_body):
            problems.append(
                "arm6: SoulStore.refreshCache(…) no longer bumps _writeVersion -- "
                "the documented \"rewritten outside save()\" path (the chat memory "
                "sheet's raw saveFile) stops notifying the editors"
            )

    # ---- arm 7: the Soul screen re-loads on that counter ------------------
    soul_clean = strip_comments_and_strings(soul_screen_text)
    if "writeVersion" not in soul_clean:
        problems.append(
            "arm7: SoulSettingsScreen does not observe SoulStore.writeVersion -- "
            "it loads once and then keeps editing a snapshot of the file it read "
            "at open time"
        )
    else:
        keyed = None
        for m in re.finditer(r"\bLaunchedEffect\s*\(", soul_clean):
            open_paren = soul_clean.find("(", m.start())
            depth, i, n = 1, open_paren + 1, len(soul_clean)
            while i < n and depth:
                if soul_clean[i] == "(":
                    depth += 1
                elif soul_clean[i] == ")":
                    depth -= 1
                i += 1
            if "writeVersion" in soul_clean[open_paren:i]:
                keyed = i
                break
        if keyed is None:
            problems.append(
                "arm7: no LaunchedEffect keyed on writeVersion in "
                "SoulSettingsScreen -- the reload path is not wired to the counter"
            )
        else:
            body = brace_body(soul_clean[keyed:], r"\s*")
            if "SoulStore.load(" not in body:
                problems.append(
                    "arm7: the writeVersion-keyed effect does not re-read the file "
                    "(no SoulStore.load( inside it) -- re-point this guard if the "
                    "reload moved into a helper"
                )

    # ---- arm 8: the Logs screen listener ---------------------------------
    body, raw_body = listener_body(log_screen_text)
    if not body:
        problems.append(
            "arm8: LogManagementScreen has no DisposableEffect registering an "
            "OnSharedPreferenceChangeListener -- `minis-config set logs.enabled …` "
            "or a restore leaves the Switch on the value captured at open time"
        )
    else:
        if ".unregisterOnSharedPreferenceChangeListener" not in body:
            problems.append(
                "arm8: the logs listener is never unregistered on dispose -- "
                "the prefs file is process-scoped, so the callback outlives the "
                "screen and leaks its composition scope"
            )
        if "AppLogger.PREF_NAME" not in body or "AppLogger.KEY_ENABLED" not in body:
            problems.append(
                "arm8: the logs listener does not go through AppLogger.PREF_NAME / "
                "AppLogger.KEY_ENABLED -- a re-typed prefs/key literal drifts out "
                "of AppLogger silently (the F-239 key-name shape)"
            )
        elif '"logging_prefs"' in raw_body:
            problems.append(
                'arm8: the logs listener re-spells the prefs name literal '
                '"logging_prefs" instead of AppLogger.PREF_NAME'
            )

    return problems


def check_arms(screen_text: str, prefs_text: str,
               soul_store_text: str = None,
               soul_screen_text: str = None,
               log_screen_text: str = None):
    """Return (problems, ok_summary). Four + five arms, judge on stripped code.

    Arms 5-8 are judged only when the corresponding source is supplied: the
    legacy two-file fixtures predate them, the real-tree call always passes
    all three (and main() fails closed when a file is missing).
    """
    problems = []
    body, raw_body = listener_body(screen_text)
    if not body:
        problems.append("could not locate the DisposableEffect listener block in "
                        + SCREEN_REL + " -- the registration call may have moved")
        if soul_store_text is not None or soul_screen_text is not None or log_screen_text is not None:
            problems.extend(check_stale_write_arms(
                soul_store_text or "", soul_screen_text or "", log_screen_text or ""))
        return problems, []

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

    # arms 5-8: the two stale-write screens ([da1028-B / ISSUE-04])
    if soul_store_text is not None or soul_screen_text is not None or log_screen_text is not None:
        problems.extend(check_stale_write_arms(
            soul_store_text or "", soul_screen_text or "", log_screen_text or ""))

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

    # ── arms 5-8 fixtures ([da1028-B / ISSUE-04]) ──────────────────────────
    COMPLIANT_STORE = '''
object SoulStore {
    private val _writeVersion = MutableStateFlow(0L)
    val writeVersion: StateFlow<Long> = _writeVersion.asStateFlow()

    fun refreshCache(context: Context) {
        val parsed = load(context)
        _cachedMetadata.value = parsed?.metadata ?: SoulMetadata.DEFAULT
        _writeVersion.value += 1
    }

    fun save(context: Context, file: SoulFile) {
        context.filesDir.resolve("SOUL.md").writeText(render(file))
        _cachedMetadata.value = file.metadata
        _writeVersion.value += 1
    }
}
'''
    COMPLIANT_SOUL_SCREEN = '''
@Composable
fun SoulSettingsScreen(onBack: () -> Unit) {
    val writeVersion by SoulStore.writeVersion.collectAsState()
    var loaded by remember { mutableStateOf(false) }
    LaunchedEffect(writeVersion) {
        val parsed = SoulStore.load(context) ?: SoulFile.defaults()
        loaded = true
    }
}
'''
    COMPLIANT_LOG_SCREEN = '''
@Composable
fun LogManagementScreen(context: Context) {
    var loggingEnabled by remember { mutableStateOf(AppLogger.isEnabled(context)) }
    DisposableEffect(context) {
        val prefs = context.applicationContext.getSharedPreferences(
            AppLogger.PREF_NAME,
            Context.MODE_PRIVATE,
        )
        val listener =
            android.content.SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
                if (key == AppLogger.KEY_ENABLED) {
                    loggingEnabled = AppLogger.isEnabled(context)
                }
            }
        prefs.registerOnSharedPreferenceChangeListener(listener)
        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }
    }
}
'''
    S5_STORE = COMPLIANT_STORE.replace(
        "        _cachedMetadata.value = file.metadata\n        _writeVersion.value += 1",
        "        _cachedMetadata.value = file.metadata")
    S6_STORE = COMPLIANT_STORE.replace(
        """        _cachedMetadata.value = parsed?.metadata ?: SoulMetadata.DEFAULT
        _writeVersion.value += 1""",
        "        _cachedMetadata.value = parsed?.metadata ?: SoulMetadata.DEFAULT")
    S7_SCREEN = COMPLIANT_SOUL_SCREEN.replace("LaunchedEffect(writeVersion)", "LaunchedEffect(Unit)")
    S8A_SCREEN = COMPLIANT_LOG_SCREEN.replace(
        "        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }\n", "")
    S8B_SCREEN = COMPLIANT_LOG_SCREEN.replace("AppLogger.PREF_NAME", '"logging_prefs"')
    S8C_SCREEN = COMPLIANT_LOG_SCREEN

    def arm(name, want, s_store, s_screen, l_screen):
        problems, _ = check_arms(COMPLIANT_SCREEN, COMPLIANT_PREFS, s_store, s_screen, l_screen)
        red = len(problems) > 0
        mark = "ok" if red == bool(want) else "FAIL"
        print(f"  selftest arm5-8 {name}: {'RED' if red else 'GREEN'} "
              f"(expect {'RED' if want else 'GREEN'}) {mark}")
        for p in problems:
            print(f"      - {p}")
        return mark == "ok"

    ok &= arm("compliant stale-write surfaces", 0,
              COMPLIANT_STORE, COMPLIANT_SOUL_SCREEN, COMPLIANT_LOG_SCREEN)
    ok &= arm("arm5 save() stops bumping writeVersion", 1,
              S5_STORE, COMPLIANT_SOUL_SCREEN, COMPLIANT_LOG_SCREEN)
    ok &= arm("arm6 refreshCache() stops bumping writeVersion", 1,
              S6_STORE, COMPLIANT_SOUL_SCREEN, COMPLIANT_LOG_SCREEN)
    ok &= arm("arm7 soul screen loads once (LaunchedEffect(Unit))", 1,
              COMPLIANT_STORE, S7_SCREEN, COMPLIANT_LOG_SCREEN)
    ok &= arm("arm8a listener never unregistered", 1,
              COMPLIANT_STORE, COMPLIANT_SOUL_SCREEN, S8A_SCREEN)
    ok &= arm("arm8b prefs name re-spelled as a literal", 1,
              COMPLIANT_STORE, COMPLIANT_SOUL_SCREEN, S8B_SCREEN)
    ok &= arm("arm8c listener block absent (fail-closed)", 1,
              COMPLIANT_STORE, COMPLIANT_SOUL_SCREEN, S8C_SCREEN.replace(
                  "DisposableEffect(context) {", "if (true) {"))
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
    soul_store_path = ROOT / SOUL_STORE_REL
    soul_screen_path = ROOT / SOUL_SCREEN_REL
    log_screen_path = ROOT / LOG_SCREEN_REL
    for rel, p in ((SCREEN_REL, screen_path), (PREFS_REL, prefs_path),
                   (SOUL_STORE_REL, soul_store_path),
                   (SOUL_SCREEN_REL, soul_screen_path),
                   (LOG_SCREEN_REL, log_screen_path)):
        if not p.exists():
            print(f"FAIL: missing {rel}")
            return 1
    problems, _ = check_arms(screen_path.read_text(encoding="utf-8", errors="replace"),
                             prefs_path.read_text(encoding="utf-8", errors="replace"),
                             soul_store_path.read_text(encoding="utf-8", errors="replace"),
                             soul_screen_path.read_text(encoding="utf-8", errors="replace"),
                             log_screen_path.read_text(encoding="utf-8", errors="replace"))
    if problems:
        print("FAIL: settings listener / stale-write gaps")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: chat menu prefs listener matches both fixed keys, "
          "re-reads all three orders inside the draggingEntry guard, "
          "both key constants are non-private, and both stale-write screens "
          "re-seed on external writes (7 sites checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
