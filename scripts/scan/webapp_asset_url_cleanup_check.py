#!/usr/bin/env python3
"""webapp_asset_url_cleanup_check — [da1021-F] gate, extended by [da1032-A].

Pins the webapp asset-URL single-source wiring and the AddToHomeSheet
cleanup closure:

  arm1: WebAppActivity.kt must not paste `file.name` raw into the
        asset-loader URL; it must route through `toAssetLoaderUrl(`.
  arm2: WebAppActivity.kt must not carry a second implementation of the
        asset path encoding (URLEncoder/Uri.encode) — the encoding comes
        from the single source (ui/preview/WebViewHolder.kt) only.
  arm3: AddToHomeSheet.kt must funnel cleanup through a DisposableEffect
        onDispose (universal hook) or an explicit Cancel cleanup.
  arm4: AddToHomeSheet.kt pin-failure branch must delete the DB row
        (`webAppShortcutRepository.delete(entity.id)`).

[da1032-A] ISSUE-01 — the fullscreen toggle used to have TWO sources (a
plain `Boolean` field on the Activity plus a `remember(initialFullscreen)`
copy inside `WebAppHost`). Two arms pin the *decision point* instead of the
call shape:

  arm5: exactly ONE fullscreen snapshot state is declared in the file (any
        `*fullscreen*`-named `= mutableStateOf(...)`), no plain
        `var/val isFullscreen` boolean field, no residual `initialFullscreen`
        token, no delegated `var *fullscreen* by remember`, and no
        `remember(...)` keyed on fullscreen.
  arm6: the composition observes and the callback writes that SAME state:
        the `WebAppHost(...)` call passes `fullscreen = <that state>.value`
        and a `onFullscreenChange = { … }` writer assigns
        `<that state>.value =`, and the `fun WebAppHost(` signature carries
        no `initialFullscreen` parameter.

Deliberately NOT flagged (precision): a `fullscreen: Boolean` parameter of
the private `FloatingToolbar` composable, and `fullscreen` locals in other
files — the arms are keyed on "a fullscreen-named snapshot state", not on the
word `fullscreen` itself.

Usage:
  python3 webapp_asset_url_cleanup_check.py <repo_root>   # real tree
  python3 webapp_asset_url_cleanup_check.py --selftest    # fixtures

Selftest marker: WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS
"""

import os
import re
import shutil
import sys
import tempfile

ACTIVITY_REL = "src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppActivity.kt"
SHEET_REL = "src/android/app/src/main/java/com/rikkaminis/app/webapp/AddToHomeSheet.kt"

# --- compliant / violation fixture snippets (code-level, not comments) ---

COMPLIANT_ACTIVITY = """package com.rikkaminis.app.webapp

import com.rikkaminis.app.ui.preview.ASSET_LOADER_HOST
import com.rikkaminis.app.ui.preview.toAssetLoaderUrl

class WebAppActivity {
    private val fullscreenState = mutableStateOf(false)

    private fun handleOnBackPressed() {
        if (fullscreenState.value) {
            fullscreenState.value = false
            applySystemBars(fullscreen = false)
        } else {
            finish()
        }
    }

    private fun renderHost() {
        setContent {
            WebAppHost(
                file = file,
                fullscreen = fullscreenState.value,
                onFullscreenChange = { fs ->
                    fullscreenState.value = fs
                    applySystemBars(fullscreen = fs)
                },
                onClose = { finish() },
            )
        }
    }

    @Composable
    private fun WebAppHost(
        file: File,
        fullscreen: Boolean,
        onFullscreenChange: (Boolean) -> Unit,
        onClose: () -> Unit,
    ) {
        var toolbarVisible by remember { mutableStateOf(true) }
        Box {
            FloatingToolbar(
                fullscreen = fullscreen,
                onToggleFullscreen = { onFullscreenChange(!fullscreen) },
            )
        }
    }

    // `fullscreen` as a *parameter* is legal and must not be flagged.
    @Composable
    private fun FloatingToolbar(
        fullscreen: Boolean,
        onToggleFullscreen: () -> Unit,
    ) {
        val icon = if (fullscreen) FullscreenExit else Fullscreen
    }

    private fun loadHtml(webView: WebView, file: File) {
        val url = toAssetLoaderUrl("file://" + file.absolutePath)
            ?: run {
                renderSourceMissing(sourceSessionId = null)
                return
            }
        webView.loadUrl(url)
    }
    private fun isAssetHost(url: Uri): Boolean =
        url.host == ASSET_LOADER_HOST
}
"""

VIOLATION_ARM1 = COMPLIANT_ACTIVITY.replace(
    'toAssetLoaderUrl("file://" + file.absolutePath)',
    '"https://" + ASSET_LOADER_HOST + "/${file.name}"',
)

VIOLATION_ARM2 = COMPLIANT_ACTIVITY.replace(
    'toAssetLoaderUrl("file://" + file.absolutePath)',
    'Uri.encode(file.absolutePath).let { "https://" + ASSET_LOADER_HOST + "/" + it }',
)

# [da1032-A] the pre-fix shape, restored verbatim: plain Boolean field +
# keyed remember copy inside the host.
VIOLATION_ARM5_DOUBLE_SOURCE = COMPLIANT_ACTIVITY.replace(
    "    private val fullscreenState = mutableStateOf(false)\n",
    "    private var isFullscreen: Boolean = false\n",
).replace(
    "        var toolbarVisible by remember { mutableStateOf(true) }\n",
    "        var fullscreen by remember(initialFullscreen) { mutableStateOf(initialFullscreen) }\n"
    "        var toolbarVisible by remember { mutableStateOf(true) }\n",
).replace(
    "                fullscreen = fullscreenState.value,\n",
    "                initialFullscreen = isFullscreen,\n",
)

# A *second* copy, correctly keyed — the subtle regression shape.
VIOLATION_ARM5_SECOND_COPY = COMPLIANT_ACTIVITY.replace(
    "        var toolbarVisible by remember { mutableStateOf(true) }\n",
    "        var fullscreen by remember { mutableStateOf(false) }\n"
    "        var toolbarVisible by remember { mutableStateOf(true) }\n",
)

# arm6-only: the state exists and is read, but the callback never writes it
# (icon and system bars drift apart after a back press).
VIOLATION_ARM6_SILENT_CALLBACK = COMPLIANT_ACTIVITY.replace(
    "                    fullscreenState.value = fs\n",
    "",
)

COMPLIANT_SHEET = """package com.rikkaminis.app.webapp

@Composable
fun AddToHomeSheet(source: WebAppSource, onDismiss: () -> Unit) {
    val cleanupCopiedAttachment = {
        if (!pinned) f.delete()
        Unit
    }
    DisposableEffect(Unit) {
        onDispose { cleanupCopiedAttachment() }
    }
    OutlinedButton(onClick = onDismiss) { Text(stringResource(R.string.cancel)) }
    scope.launch {
        if (ok) {
            pinned = true
        } else {
            runCatching { app.webAppShortcutRepository.delete(entity.id) }
            cleanupCopiedAttachment()
        }
        onDismiss()
    }
}
"""


def strip_comments(src: str) -> str:
    """Remove //-comments, /* */ comments and KDoc, keeping line structure.

    String-aware: this file carries string literals containing `//`
    (`"file://" + file.absolutePath`) and template literals with embedded
    quotes — a naive per-line `line.find("//")` truncation mangles them.
    """
    out = []
    i = 0
    n = len(src)
    in_string = None
    while i < n:
        c = src[i]
        if in_string is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c == '"':
            in_string = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            if j == -1:
                j = n
            out.append("")
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            end = n if j == -1 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:end]))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --- [da1032-A] fullscreen single-source patterns -------------------------

# A snapshot-state declaration: `(private )? val|var <name> = (androidx...)?mutableStateOf(`
# (the `by remember { mutableStateOf(...) }` delegate form is matched below).
FULLSCREEN_STATE_DECL_RE = re.compile(
    r"\b(?:private\s+)?(?:var|val)\s+([A-Za-z_]\w*)\s*=\s*"
    r"(?:androidx\.compose\.runtime\.)?mutableStateOf\("
)
PLAIN_BOOL_FIELD_RE = re.compile(r"\b(?:var|val)\s+isFullscreen\b")
DELEGATED_VAR_RE = re.compile(r"\bvar\s+([A-Za-z_]\w*)\s+by\s+remember\b")
KEYED_REMEMBER_RE = re.compile(r"\bremember\s*\([^)]*[Ff]ullscreen")


def _fullscreen_states(code: str):
    return [
        m.group(1)
        for m in FULLSCREEN_STATE_DECL_RE.finditer(code)
        if "fullscreen" in m.group(1).lower()
    ]


def _paren_block(code: str, open_idx: int) -> str:
    depth = 0
    for j in range(open_idx, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return code[open_idx:j + 1]
    return code[open_idx:]


def call_blocks(code: str, call_name: str):
    """`callName(...)` blocks that are CALLS, not the `fun callName(` decl."""
    blocks = []
    for m in re.finditer(r"\b" + re.escape(call_name) + r"\s*\(", code):
        if re.search(r"\bfun\s+$", code[max(0, m.start() - 12):m.start()]):
            continue
        blocks.append(_paren_block(code, code.index("(", m.start())))
    return blocks


def fun_signature(code: str, fun_name: str):
    m = re.search(r"\bfun\s+" + re.escape(fun_name) + r"\s*\(", code)
    if not m:
        return None
    return code[max(0, m.start() - 24):m.start()] + _paren_block(
        code, code.index("(", m.start())
    )


def check_activity_arm1(src: str) -> bool:
    """No bare `${file.name}` interpolation into the asset-loader URL, and
    the loadUrl path routes through toAssetLoaderUrl."""
    code = strip_comments(src)
    if '"/${file.name}"' in code or "/${file.name}" in code:
        return False
    return "toAssetLoaderUrl(" in code


def check_activity_arm2(src: str) -> bool:
    """No second implementation of the *asset path* encoding: the encoding
    must come from the single source (toAssetLoaderUrl). A pre-existing
    Uri.encode on a session deep link (minis://) is legitimate and stays."""
    code = strip_comments(src)
    if "URLEncoder.encode(" in code or "Uri.encode(file." in code:
        return False
    return "toAssetLoaderUrl(" in code


def check_activity_arm5(src: str) -> bool:
    """[da1032-A] Exactly one fullscreen snapshot state, and no second copy:
    no plain `isFullscreen` boolean field, no residual `initialFullscreen`,
    no delegated `var <fullscreen-ish> by remember`, no remember keyed on
    fullscreen."""
    code = strip_comments(src)
    if PLAIN_BOOL_FIELD_RE.search(code):
        return False
    if "initialFullscreen" in code:
        return False
    if len(_fullscreen_states(code)) != 1:
        return False
    for m in DELEGATED_VAR_RE.finditer(code):
        if "fullscreen" in m.group(1).lower():
            return False
    if KEYED_REMEMBER_RE.search(code):
        return False
    return True


def check_activity_arm6(src: str) -> bool:
    """[da1032-A] The host observes and the callback writes the SAME state
    object; the host signature carries no second fullscreen input."""
    code = strip_comments(src)
    names = _fullscreen_states(code)
    if len(names) != 1:
        return False
    name = names[0]
    blocks = call_blocks(code, "WebAppHost")
    if not blocks:
        return False
    read = re.compile(r"\bfullscreen\s*=\s*" + re.escape(name) + r"\.value\b")
    write = re.compile(re.escape(name) + r"\.value\s*=")
    if not any(read.search(b) and write.search(b) for b in blocks):
        return False
    sig = fun_signature(code, "WebAppHost")
    if sig is None or "initialFullscreen" in sig:
        return False
    return True


def check_sheet_arm3(src: str) -> bool:
    """The universal hook must funnel cleanup: a DisposableEffect onDispose
    referencing cleanupCopiedAttachment is the accepted shape; a file
    without any DisposableEffect must clean up in the Cancel callback."""
    code = strip_comments(src)
    if "cleanupCopiedAttachment()" not in code:
        return False
    if "onDispose { cleanupCopiedAttachment() }" in code:
        return True
    return "DisposableEffect(" not in code and "onClick = { cleanupCopiedAttachment()" in code


def check_sheet_arm4(src: str) -> bool:
    """Pin-failure branch must delete the DB row."""
    code = strip_comments(src)
    return "webAppShortcutRepository.delete(entity.id)" in code


def run_on_tree(root: str) -> int:
    failures = []
    act_path = os.path.join(root, ACTIVITY_REL)
    sheet_path = os.path.join(root, SHEET_REL)
    for label, path in (("WebAppActivity.kt", act_path), ("AddToHomeSheet.kt", sheet_path)):
        if not os.path.exists(path):
            print(f"[webapp-asset-url] FAIL missing file: {label}")
            return 1
    act = open(act_path, encoding="utf-8").read()
    sheet = open(sheet_path, encoding="utf-8").read()

    if not check_activity_arm1(act):
        failures.append("arm1")
    if not check_activity_arm2(act):
        failures.append("arm2")
    if not check_sheet_arm3(sheet):
        failures.append("arm3")
    if not check_sheet_arm4(sheet):
        failures.append("arm4")
    if not check_activity_arm5(act):
        failures.append("arm5")
    if not check_activity_arm6(act):
        failures.append("arm6")

    if failures:
        print(f"[webapp-asset-url] FAIL arms={','.join(failures)}")
        return 1
    print("[webapp-asset-url] PASS all 6 arms "
          "(asset-url single source ×2, sheet cleanup ×2, fullscreen single source ×2)")
    return 0


def selftest() -> int:
    tmp = tempfile.mkdtemp(prefix="webapp-gate-")
    ok = True
    try:
        cases = [
            ("compliant", COMPLIANT_ACTIVITY, COMPLIANT_SHEET, True),
            ("arm1-violation", VIOLATION_ARM1, COMPLIANT_SHEET, False),
            ("arm2-violation", VIOLATION_ARM2, COMPLIANT_SHEET, False),
            ("arm3-violation", COMPLIANT_ACTIVITY, COMPLIANT_SHEET.replace(
                "onDispose { cleanupCopiedAttachment() }", "onDispose { onDismiss() }"), False),
            ("arm4-violation", COMPLIANT_ACTIVITY, COMPLIANT_SHEET.replace(
                "runCatching { app.webAppShortcutRepository.delete(entity.id) }", "cleanupOnly()"), False),
            ("arm5-violation double source (plain field + keyed copy)",
             VIOLATION_ARM5_DOUBLE_SOURCE, COMPLIANT_SHEET, False),
            ("arm5-violation second remember copy",
             VIOLATION_ARM5_SECOND_COPY, COMPLIANT_SHEET, False),
            ("arm6-violation callback never writes the state",
             VIOLATION_ARM6_SILENT_CALLBACK, COMPLIANT_SHEET, False),
        ]
        for name, act, sheet, expected_pass in cases:
            tree = os.path.join(tmp, name.replace(" ", "_").replace("(", "").replace(")", ""))
            act_dir = os.path.join(tree, os.path.dirname(ACTIVITY_REL))
            sheet_dir = os.path.join(tree, os.path.dirname(SHEET_REL))
            os.makedirs(act_dir, exist_ok=True)
            os.makedirs(sheet_dir, exist_ok=True)
            open(os.path.join(tree, ACTIVITY_REL), "w", encoding="utf-8").write(act)
            open(os.path.join(tree, SHEET_REL), "w", encoding="utf-8").write(sheet)
            rc = run_on_tree(tree)
            passed = rc == 0
            if passed != expected_pass:
                print(f"[webapp-asset-url] SELFTEST FAIL case={name} "
                      f"expected={'pass' if expected_pass else 'fail'} got={passed}")
                ok = False
            else:
                print(f"[webapp-asset-url] selftest ok case={name} ({'pass' if passed else 'fail'})")
        # [da1032-A] Precision: the arms must key on the decision point, not on
        # "anything with the word fullscreen goes red". Each *targeted* violation
        # trips its own arm and leaves the other green. The pre-fix double-source
        # shape is the one case that legitimately trips BOTH (there is no single
        # state left for arm6 to observe) — recorded as such, not hidden.
        for name, act, want in (
            ("double source (no single state left)", VIOLATION_ARM5_DOUBLE_SOURCE, (True, True)),
            ("second remember copy", VIOLATION_ARM5_SECOND_COPY, (True, False)),
            ("silent callback", VIOLATION_ARM6_SILENT_CALLBACK, (False, True)),
        ):
            bad5 = not check_activity_arm5(act)
            bad6 = not check_activity_arm6(act)
            if (bad5, bad6) == want:
                print(f"[webapp-asset-url] selftest ok precision {name}: "
                      f"arm5 red={bad5}, arm6 red={bad6}")
            else:
                print(f"[webapp-asset-url] SELFTEST FAIL precision {name}: "
                      f"got arm5 red={bad5}, arm6 red={bad6} (want {want})")
                ok = False
        if ok:
            print("WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS")
            return 0
        print("WEBAPP_ASSET_URL_CLEANUP_SELFTEST=FAIL")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return run_on_tree(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
