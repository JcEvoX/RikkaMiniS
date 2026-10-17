#!/usr/bin/env python3
"""webapp_asset_url_cleanup_check — [da1021-F] gate.

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

Usage:
  python3 webapp_asset_url_cleanup_check.py <repo_root>   # real tree
  python3 webapp_asset_url_cleanup_check.py --selftest    # fixtures

Selftest marker: WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS
"""

import os
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
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


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

    if failures:
        print(f"[webapp-asset-url] FAIL arms={','.join(failures)}")
        return 1
    print("[webapp-asset-url] PASS all 4 arms")
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
        ]
        for name, act, sheet, expected_pass in cases:
            tree = os.path.join(tmp, name)
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
