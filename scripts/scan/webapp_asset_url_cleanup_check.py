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

[dv14-B2] ISSUE-07 — the title field used to start blank and be filled by a
first-frame `LaunchedEffect` from the de-suffixed file name; the async parsed
`<title>` then arrived to a non-blank field and was dropped 100% of the time.
arm7 pins the decision point (only AddToHomeSheet.kt):

  arm7: the title field is seeded from the file name (`var titleText by
        remember(<fileName>)` with the seed deriving from fileName), and the
        `LaunchedEffect(parsedTitle)` backfill is guarded by a user-edited
        flag that the text-field writer flips (`userEditedTitle = true`) —
        so a late parse can neither win the race against the seed nor
        overwrite what the user typed.

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

[dv21-B04 B-08] the pin verdict was silent on two carriers and the tile
decode ran on the composition thread. Three anchors, all on the decision
point (never on the call spelling):

  arm8a (ShortcutPinner.kt) / arm8b (WebPreviewShortcut.kt): every
        `requestPinShortcut` call is CAPTURED (its nearest enclosing
        `runCatching { }` spans the call, so a throwing OEM launcher cannot
        take the process down), its verdict is classified once through the
        shared `pinOutcome(`, compared against `ShortcutPinOutcome.REQUESTED`,
        and a non-REQUESTED answer reaches the user (`webapp_pin_failed`) —
        `false` and a throw are the same answer to the user.
  arm9: AddToHomeSheet.kt — no decode (`decodeFileBounded(` /
        `loadBitmapFromUri(`) inside a `remember(...) { }` block (that is the
        composition thread), and every decode that lives in a `produceState`
        block hops off it (`Dispatchers.IO` in the same block). Fail-closed
        when the sheet carries no decode call at all (anchor moved).

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
PINNER_REL = "src/android/app/src/main/java/com/rikkaminis/app/webapp/ShortcutPinner.kt"
PREVIEW_SHORTCUT_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewShortcut.kt"
)

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
    var parsedTitle by remember { mutableStateOf<String?>(null) }
    var titleText by remember(fileName) { mutableStateOf(webAppTitleFallback(fileName)) }
    var userEditedTitle by remember { mutableStateOf(false) }
    LaunchedEffect(parsedTitle) {
        webAppTitleBackfill(parsedTitle, userEditedTitle)?.let { titleText = it }
    }
    OutlinedTextField(
        value = titleText,
        onValueChange = {
            userEditedTitle = true
            titleText = sanitizeSingleLineInput(it)
        },
    )
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

# [dv14-B2] arm7 violations (sheet only; arm3/arm4 anchors untouched):
#  - the backfill effect ignores the user-edited guard (a late parse wins);
#  - the text-field writer never flips the guard;
#  - the field is seeded empty again (the pre-fix race).
VIOLATION_ARM7_UNGUARDED_BACKFILL = COMPLIANT_SHEET.replace(
    "        webAppTitleBackfill(parsedTitle, userEditedTitle)?.let { titleText = it }",
    "        titleText = parsedTitle ?: titleText",
)
VIOLATION_ARM7_WRITER_NO_FLAG = COMPLIANT_SHEET.replace(
    "            userEditedTitle = true\n",
    "",
)
VIOLATION_ARM7_EMPTY_SEED = COMPLIANT_SHEET.replace(
    "mutableStateOf(webAppTitleFallback(fileName))",
    'mutableStateOf("")',
)


# [dv21-B04 B-08] compliant carriers: one shared verdict, captured call, the
# decline surfaced. Code-level shapes only.
COMPLIANT_PINNER = """package com.rikkaminis.app.webapp

object ShortcutPinner {
    fun pin(context: Context, shortcut: WebAppShortcutEntity): Boolean {
        val outcome = pinOutcome(
            runCatching { ShortcutManagerCompat.requestPinShortcut(context, info, null) },
        )
        if (outcome != ShortcutPinOutcome.REQUESTED) {
            Toast.makeText(context, R.string.webapp_pin_failed, Toast.LENGTH_LONG).show()
        }
        return outcome == ShortcutPinOutcome.REQUESTED
    }
}
"""

COMPLIANT_PREVIEW_SHORTCUT = """package com.rikkaminis.app.ui.preview

object WebPreviewShortcut {
    fun pin(context: Context, title: String): Boolean {
        val outcome = pinOutcome(
            runCatching { ShortcutManagerCompat.requestPinShortcut(context, info, null) },
        )
        Toast.makeText(
            context,
            context.getString(
                if (outcome == ShortcutPinOutcome.REQUESTED) {
                    R.string.webpreview_pin_to_home_requested
                } else {
                    R.string.webapp_pin_failed
                },
            ),
            Toast.LENGTH_SHORT,
        ).show()
        return outcome == ShortcutPinOutcome.REQUESTED
    }
}
"""

# [dv21-B04 B-08] arm8 violations (one per carrier, one criterion each):
#  - the pinner classifies nothing: the verdict is a raw Boolean again, so a
#    decline is silent (and a throw escapes);
#  - the preview carrier keeps the verdict but drops the capture, so an OEM
#    launcher that throws kills the process.
VIOLATION_ARM8_SILENT_DECLINE = COMPLIANT_PINNER.replace(
    """        val outcome = pinOutcome(
            runCatching { ShortcutManagerCompat.requestPinShortcut(context, info, null) },
        )""",
    """        val outcome = ShortcutManagerCompat.requestPinShortcut(context, info, null)""",
).replace(
    """        if (outcome != ShortcutPinOutcome.REQUESTED) {
            Toast.makeText(context, R.string.webapp_pin_failed, Toast.LENGTH_LONG).show()
        }
        return outcome == ShortcutPinOutcome.REQUESTED""",
    """        return outcome""",
)
VIOLATION_ARM8_NO_CAPTURE = COMPLIANT_PREVIEW_SHORTCUT.replace(
    "runCatching { ShortcutManagerCompat.requestPinShortcut(context, info, null) }",
    "ShortcutManagerCompat.requestPinShortcut(context, info, null)",
)

# [dv21-B04 B-08] arm9 fixture fragment (compliant) + its two violations.
SHEET_DECODE_TILE = """
@Composable
private fun IconTile(path: String) {
    val bmp by produceState<Bitmap?>(initialValue = null, path) {
        value = withContext(Dispatchers.IO) {
            ImageDecoding.decodeFileBounded(path, ICON_MAX_EDGE_PX)?.bitmap
        }
    }
    if (bmp != null) {
        Image(bitmap = bmp.asImageBitmap(), contentDescription = null)
    }
}
"""

VIOLATION_ARM9_REMEMBER = SHEET_DECODE_TILE.replace(
    """    val bmp by produceState<Bitmap?>(initialValue = null, path) {
        value = withContext(Dispatchers.IO) {
            ImageDecoding.decodeFileBounded(path, ICON_MAX_EDGE_PX)?.bitmap
        }
    }""",
    """    val bmp = remember(path) {
        ImageDecoding.decodeFileBounded(path, ICON_MAX_EDGE_PX)?.bitmap
    }""",
)
VIOLATION_ARM9_NO_IO = SHEET_DECODE_TILE.replace(
    """        value = withContext(Dispatchers.IO) {
            ImageDecoding.decodeFileBounded(path, ICON_MAX_EDGE_PX)?.bitmap
        }""",
    """        value = ImageDecoding.decodeFileBounded(path, ICON_MAX_EDGE_PX)?.bitmap""",
)


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


def _brace_block(code: str, open_idx: int) -> str:
    depth = 0
    for j in range(open_idx, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[open_idx:j + 1]
    return code[open_idx:]


def check_sheet_arm7(src: str) -> bool:
    """[dv14-B2] Title seed + guarded backfill (AddToHomeSheet.kt only).

    Decision points, not call shapes:
      - the `titleText` snapshot state is seeded from the file name (so the
        very first composition already shows the fallback title and a slow
        async parse cannot win the race by blanking the field);
      - the text-field writer flips the user-edited guard;
      - the `LaunchedEffect(parsedTitle)` body is guarded by that flag.
    Fail-closed on any missing anchor."""
    code = strip_comments(src)
    if "titleText" not in code or "parsedTitle" not in code:
        return False
    m = re.search(r"\bvar\s+titleText\s+by\s+remember\s*\(", code)
    if not m:
        return False
    open_idx = code.index("(", m.start())
    seed = _paren_block(code, open_idx)
    seed_end = open_idx + len(seed)
    init_brace = code.find("{", seed_end)
    init = _brace_block(code, init_brace) if init_brace >= 0 else ""
    if "fileName" not in init:
        return False
    if "userEditedTitle" not in code:
        return False
    if not re.search(r"userEditedTitle\s*=\s*true", code):
        return False
    eff = re.search(r"LaunchedEffect\s*\(\s*parsedTitle[^)]*\)", code)
    if not eff:
        return False
    brace = code.find("{", eff.end())
    body = _brace_block(code, brace) if brace >= 0 else ""
    return "userEditedTitle" in body


DECODE_CALLS = ("decodeFileBounded", "loadBitmapFromUri")


def _call_sites(code: str, name: str):
    """Offsets of `name(` CALLS (the `fun name(` declaration is skipped)."""
    out = []
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", code):
        if re.search(r"\bfun\s+$", code[max(0, m.start() - 12):m.start()]):
            continue
        out.append(m.start())
    return out


def _block_body(code: str, header_re: str):
    """(open_brace, body) for each `header_re` occurrence that opens a lambda."""
    out = []
    for m in re.finditer(header_re, code):
        ob = code.find("(", m.end() - 1) if header_re.endswith(r"\(") else code.find("(", m.start())
        if ob < 0:
            continue
        pb = _paren_block(code, ob)
        br = code.find("{", ob + len(pb))
        if br < 0:
            continue
        body = _brace_block(code, br)
        if not body.endswith("}"):
            continue
        out.append((br, body))
    return out


def check_pin_arm8(src: str) -> bool:
    """[dv21-B04 B-08] ONE captured pin attempt whose verdict is surfaced.

    Decision points, not call shapes:
      - a throwing OEM launcher cannot escape: the call sits inside a
        `runCatching { }` block;
      - the answer is classified once by the shared `pinOutcome(` and compared
        against `REQUESTED` (a bare Boolean is not a verdict — `false` and a
        throw are the same answer to the user);
      - a non-REQUESTED answer is visible: the failure string is referenced.
    Fail-closed when the carrier no longer calls `requestPinShortcut`."""
    code = strip_comments(src)
    sites = _call_sites(code, "requestPinShortcut")
    if not sites:
        return False
    captured_spans = []
    for m in re.finditer(r"\brunCatching\s*\{", code):
        br = code.index("{", m.start())
        body = _brace_block(code, br)
        if body.endswith("}"):
            captured_spans.append((br, br + len(body)))
    for s in sites:
        if not any(a < s < b for a, b in captured_spans):
            return False
    if "pinOutcome(" not in code:
        return False
    if not re.search(r"ShortcutPinOutcome\s*\.\s*REQUESTED", code):
        return False
    return "webapp_pin_failed" in code


def check_sheet_arm9(src: str) -> bool:
    """[dv21-B04 B-08] No decode on the composition thread.

    Two decision points:
      - nothing decodes inside a `remember(...) { }` body (that body runs on
        the composition thread — the sheet used to block for the whole
        read+sample of a 12 Mpx icon);
      - any decode in a `produceState` body hops off it (`Dispatchers.IO` in
        the same body; `produceState` otherwise still runs on the composition
        dispatcher).
    Fail-closed when the sheet carries no decode call at all (anchor moved)."""
    code = strip_comments(src)
    sites = []
    for name in DECODE_CALLS:
        sites.extend(_call_sites(code, name))
    if not sites:
        return False
    for br, body in _block_body(code, r"\bremember\s*\("):
        if any(br < s < br + len(body) for s in sites):
            return False
    for br, body in _block_body(code, r"\bproduceState\s*(?:<[^>{]*>)?\s*\("):
        if any(br < s < br + len(body) for s in sites):
            if not re.search(r"Dispatchers\s*\.\s*IO", body):
                return False
    return True


def run_on_tree(root: str) -> int:
    failures = []
    act_path = os.path.join(root, ACTIVITY_REL)
    sheet_path = os.path.join(root, SHEET_REL)
    pinner_path = os.path.join(root, PINNER_REL)
    preview_path = os.path.join(root, PREVIEW_SHORTCUT_REL)
    for label, path in (
        ("WebAppActivity.kt", act_path),
        ("AddToHomeSheet.kt", sheet_path),
        ("ShortcutPinner.kt", pinner_path),
        ("WebPreviewShortcut.kt", preview_path),
    ):
        if not os.path.exists(path):
            print(f"[webapp-asset-url] FAIL missing file: {label}")
            return 1
    act = open(act_path, encoding="utf-8").read()
    sheet = open(sheet_path, encoding="utf-8").read()
    pinner = open(pinner_path, encoding="utf-8").read()
    preview = open(preview_path, encoding="utf-8").read()

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
    if not check_sheet_arm7(sheet):
        failures.append("arm7")
    if not check_pin_arm8(pinner):
        failures.append("arm8a")
    if not check_pin_arm8(preview):
        failures.append("arm8b")
    if not check_sheet_arm9(sheet):
        failures.append("arm9")

    if failures:
        print(f"[webapp-asset-url] FAIL arms={','.join(failures)}")
        return 1
    print("[webapp-asset-url] PASS all 10 arms "
          "(asset-url single source ×2, sheet cleanup ×2, fullscreen single source ×2, "
          "title seed + user-edit guard ×1, pin verdict ×2, decode off the "
          "composition thread ×1)")
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
            ("arm7-violation unguarded backfill overwrites the field",
             COMPLIANT_ACTIVITY, VIOLATION_ARM7_UNGUARDED_BACKFILL, False),
            ("arm7-violation writer never flips the guard",
             COMPLIANT_ACTIVITY, VIOLATION_ARM7_WRITER_NO_FLAG, False),
            ("arm7-violation empty seed (pre-fix race)",
             COMPLIANT_ACTIVITY, VIOLATION_ARM7_EMPTY_SEED, False),
        ]
        def write_tree(name, act, sheet, pinner, preview):
            tree = os.path.join(tmp, name.replace(" ", "_").replace("(", "").replace(")", ""))
            for rel in (ACTIVITY_REL, SHEET_REL, PINNER_REL, PREVIEW_SHORTCUT_REL):
                os.makedirs(os.path.join(tree, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(tree, ACTIVITY_REL), "w", encoding="utf-8").write(act)
            open(os.path.join(tree, SHEET_REL), "w", encoding="utf-8").write(
                sheet + SHEET_DECODE_TILE)
            open(os.path.join(tree, PINNER_REL), "w", encoding="utf-8").write(pinner)
            open(os.path.join(tree, PREVIEW_SHORTCUT_REL), "w", encoding="utf-8").write(preview)
            return tree

        for name, act, sheet, expected_pass in cases:
            tree = write_tree(name, act, sheet, COMPLIANT_PINNER, COMPLIANT_PREVIEW_SHORTCUT)
            rc = run_on_tree(tree)
            passed = rc == 0
            if passed != expected_pass:
                print(f"[webapp-asset-url] SELFTEST FAIL case={name} "
                      f"expected={'pass' if expected_pass else 'fail'} got={passed}")
                ok = False
            else:
                print(f"[webapp-asset-url] selftest ok case={name} ({'pass' if passed else 'fail'})")
        # [dv21-B04 B-08] each targeted violation trips its OWN arm only.
        for name, pinner, preview, sheet_extra, want in (
            ("b08-pin-silent-decline", VIOLATION_ARM8_SILENT_DECLINE,
             COMPLIANT_PREVIEW_SHORTCUT, "", (True, False, False)),
            ("b08-pin-no-capture", COMPLIANT_PINNER, VIOLATION_ARM8_NO_CAPTURE,
             "", (False, True, False)),
            ("b08-decode-in-remember", COMPLIANT_PINNER, COMPLIANT_PREVIEW_SHORTCUT,
             VIOLATION_ARM9_REMEMBER, (False, False, True)),
            ("b08-decode-no-io-hop", COMPLIANT_PINNER, COMPLIANT_PREVIEW_SHORTCUT,
             VIOLATION_ARM9_NO_IO, (False, False, True)),
        ):
            tree = os.path.join(tmp, name)
            for rel in (ACTIVITY_REL, SHEET_REL, PINNER_REL, PREVIEW_SHORTCUT_REL):
                os.makedirs(os.path.join(tree, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(tree, ACTIVITY_REL), "w", encoding="utf-8").write(COMPLIANT_ACTIVITY)
            open(os.path.join(tree, SHEET_REL), "w", encoding="utf-8").write(
                COMPLIANT_SHEET + (sheet_extra or SHEET_DECODE_TILE))
            open(os.path.join(tree, PINNER_REL), "w", encoding="utf-8").write(pinner)
            open(os.path.join(tree, PREVIEW_SHORTCUT_REL), "w", encoding="utf-8").write(preview)
            got = (not check_pin_arm8(pinner), not check_pin_arm8(preview),
                   not check_sheet_arm9(
                       open(os.path.join(tree, SHEET_REL), encoding="utf-8").read()))
            others = (check_activity_arm1(COMPLIANT_ACTIVITY)
                      and check_activity_arm2(COMPLIANT_ACTIVITY)
                      and check_sheet_arm3(COMPLIANT_SHEET)
                      and check_sheet_arm4(COMPLIANT_SHEET)
                      and check_sheet_arm7(COMPLIANT_SHEET))
            if got == want and others:
                print(f"[webapp-asset-url] selftest ok {name}: "
                      f"arm8a/8b/9 red={got}, arm1-7 green")
            else:
                print(f"[webapp-asset-url] SELFTEST FAIL {name}: "
                      f"got={got} want={want} arm1-7 green={others}")
                ok = False
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
        # [dv14-B2] arm7 precision: each targeted title violation trips arm7
        # only (arm3 cleanup + arm4 pin-failure stay green).
        for name, sheet in (
            ("unguarded backfill", VIOLATION_ARM7_UNGUARDED_BACKFILL),
            ("writer without the flag", VIOLATION_ARM7_WRITER_NO_FLAG),
            ("empty seed", VIOLATION_ARM7_EMPTY_SEED),
        ):
            bad7 = not check_sheet_arm7(sheet)
            others = check_sheet_arm3(sheet) and check_sheet_arm4(sheet)
            if bad7 and others:
                print(f"[webapp-asset-url] selftest ok precision {name}: "
                      f"arm7 red={bad7}, arm3/arm4 green")
            else:
                print(f"[webapp-asset-url] SELFTEST FAIL precision {name}: "
                      f"arm7 red={bad7}, arm3/arm4 green={others}")
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
