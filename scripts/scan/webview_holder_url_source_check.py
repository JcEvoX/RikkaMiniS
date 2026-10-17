#!/usr/bin/env python3
"""deep-audit-1020 FIX-D (ISSUE-04, P1) — WebViewHolder currentUrl source gate.

Pins the contract introduced by branch fix/da1020-webview-holder-url for
ui/preview/WebViewHolder.kt: the `currentUrl` published by `onPageStarted` must
be mapped back from the WebView's internal asset-loader origin
(`https://appassets.androidplatform.net/<name>`) to the `file://` URL the host
constructed the holder with, via the single-sourced pure mapping pair
(`toAssetLoaderUrl` / `fromAssetLoaderUrl`).

Bug context: `onPageStarted` used to assign the WebView-reported URL directly
(`currentUrl = url`). For local HTML that URL is the asset-loader origin, which
silently broke three consumers that key off the user-visible URL:
  * openExternalFromSheet's FileProvider branch (gated on startsWith("file://"))
    degraded to a meaningless ACTION_VIEW of an internal URL;
  * the pin-to-home entry point (same gate) disappeared;
  * ChatScreen's holder-reuse check (existing.currentUrl != url) compared
    appassets vs file:// and destroyed+rebuilt the holder on every repeat tap.

The gate enforces:
  W1  the `currentUrl = …` assignment in onPageStarted goes through
      `fromAssetLoaderUrl(url, assetRootDir) ?: url` (slice assertion — a
      direct `currentUrl = url` revert is RED);
  W2  the load path uses the same single source
      (`toAssetLoaderUrl(currentUrl) ?: currentUrl`);
  W3  single source: the mapping pair is defined exactly once, the origin host
      constant is declared exactly once, the legacy inline `file.name`
      derivation is gone, and the percent-decoder is the existing
      ui.chat.decodeUriPath (no third implementation);
  W4  the downstream consumers still read `holder.currentUrl`
      (WebPreviewBottomSheet.kt) — guards against "fixing downstream" instead
      of the root cause.

Usage: python3 webview_holder_url_source_check.py [ROOT]   (ROOT = repo root)
       python3 webview_holder_url_source_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

HOLDER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt"
SHEET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewBottomSheet.kt"

ASSIGN_RE = re.compile(r"^\s*currentUrl\s*=\s*(.+?)\s*$")


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included).

    String-literal aware: `//` inside a string (e.g. "file://") must NOT be
    treated as a comment — the pin gate literal `holder.currentUrl.startsWith(
    "file://")` lives on a code line and would otherwise be truncated.
    """
    out = []
    in_block = False
    for line in src.splitlines():
        res = []
        i = 0
        n = len(line)
        quote = None
        while i < n:
            if in_block:
                j = line.find("*/", i)
                if j < 0:
                    i = n
                else:
                    in_block = False
                    i = j + 2
                continue
            c = line[i]
            if quote is not None:
                res.append(c)
                if c == "\\" and i + 1 < n:
                    res.append(line[i + 1])
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if line.startswith("//", i):
                break
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if c in ('"', "'"):
                quote = c
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def analyse(holder_code, sheet_code):
    """Return [(criterion, ok, detail, line_no_or_None)]."""
    code = strip_comments(holder_code)
    code_lines = code.splitlines()
    results = []

    # --- W1: the onPageStarted assignment goes through the reverse mapper ---
    assigns = [(i, m.group(1)) for i, l in enumerate(code_lines)
               if (m := ASSIGN_RE.match(l))]
    if len(assigns) != 1:
        results.append(("W1 currentUrl assignment present and unique",
                        False,
                        "expected exactly one `currentUrl = …` in code, found %d"
                        % len(assigns), None))
    else:
        line_no, rhs = assigns[0]
        ok = ("fromAssetLoaderUrl(" in rhs and "url" in rhs
              and "assetRootDir" in rhs)
        results.append(("W1 onPageStarted publishes via fromAssetLoaderUrl(url, assetRootDir)",
                        ok,
                        "RHS = %r" % rhs, line_no + 1))

    # --- W2: the load path uses the forward mapper (single source) ---
    ok2 = "toAssetLoaderUrl(currentUrl) ?: currentUrl" in code
    results.append(("W2 load path uses toAssetLoaderUrl(currentUrl) ?: currentUrl",
                    ok2, "load site must reuse the same mapping", None))

    # --- W3: single source for the mapping pair / origin / decode ---
    n_to = len(re.findall(r"\bfun\s+toAssetLoaderUrl\(", code))
    n_from = len(re.findall(r"\bfun\s+fromAssetLoaderUrl\(", code))
    results.append(("W3a toAssetLoaderUrl defined exactly once", n_to == 1,
                    "found %d definitions" % n_to, None))
    results.append(("W3b fromAssetLoaderUrl defined exactly once", n_from == 1,
                    "found %d definitions" % n_from, None))
    n_host = len(re.findall(r"\bconst\s+val\s+ASSET_LOADER_HOST\b", code))
    results.append(("W3c origin host constant declared exactly once", n_host == 1,
                    "found %d declarations" % n_host, None))
    legacy = "file.name" in code
    results.append(("W3d legacy inline file.name derivation removed", not legacy,
                    "file.name still present in code", None))
    n_decode = len(re.findall(r"import\s+com\.rikkaminis\.app\.ui\.chat\.decodeUriPath", code))
    results.append(("W3e percent-decode reuses ui.chat.decodeUriPath", n_decode == 1,
                    "found %d imports" % n_decode, None))

    # --- W4: downstream still reads the single source ---
    sheet = strip_comments(sheet_code)
    ok_ext = "openExternalFromSheet(context, holder.currentUrl)" in sheet
    results.append(("W4a openExternalFromSheet still reads holder.currentUrl", ok_ext,
                    "downstream must keep reading the single source", None))
    ok_pin = 'holder.currentUrl.startsWith("file://")' in sheet
    results.append(("W4b pin entry still gates on holder.currentUrl", ok_pin,
                    "downstream must keep reading the single source", None))
    return results


# ---------------------------------------------------------------- selftest ---
GOOD_HOLDER = """package probe
class H {
    var currentUrl: String = "file:///d/a.html"
    private val assetRootDir: String? = "/d"
    fun onPageStarted(url: String) {
        currentUrl = fromAssetLoaderUrl(url, assetRootDir) ?: url
    }
    fun load() {
        val loadUrl = toAssetLoaderUrl(currentUrl) ?: currentUrl
    }
    internal const val ASSET_LOADER_HOST = "appassets.androidplatform.net"
}
internal fun toAssetLoaderUrl(fileUrl: String): String? = null
internal fun fromAssetLoaderUrl(assetUrl: String, rootDir: String?): String? = null
import com.rikkaminis.app.ui.chat.decodeUriPath
"""

BAD_HOLDER = """package probe
class H {
    var currentUrl: String = "file:///d/a.html"
    private val assetRootDir: String? = "/d"
    fun onPageStarted(url: String) {
        currentUrl = url
    }
    fun load() {
        val loadUrl = toAssetLoaderUrl(currentUrl) ?: currentUrl
    }
    internal const val ASSET_LOADER_HOST = "appassets.androidplatform.net"
}
internal fun toAssetLoaderUrl(fileUrl: String): String? = null
internal fun fromAssetLoaderUrl(assetUrl: String, rootDir: String?): String? = null
import com.rikkaminis.app.ui.chat.decodeUriPath
"""

LEGACY_HOLDER = """package probe
class H {
    var currentUrl: String = "file:///d/a.html"
    fun onPageStarted(url: String) {
        currentUrl = "https://$ASSET_LOADER_HOST/${file.name}"
    }
    fun load() {
        val loadUrl = toAssetLoaderUrl(currentUrl) ?: currentUrl
    }
}
internal const val ASSET_LOADER_HOST = "appassets.androidplatform.net"
internal fun toAssetLoaderUrl(fileUrl: String): String? = null
internal fun fromAssetLoaderUrl(assetUrl: String, rootDir: String?): String? = null
"""

GOOD_SHEET = """package probe
onOpenExternal = { openExternalFromSheet(context, holder.currentUrl) }
onPinToHome = if (holder.currentUrl.startsWith("file://")) x else null
"""

BAD_SHEET = """package probe
onOpenExternal = { openExternalFromSheet(context, holder.url) }
onPinToHome = if (holder.isLocalFile) x else null
"""


def selftest():
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    # good fixtures: everything green
    good = analyse(GOOD_HOLDER, GOOD_SHEET)
    expect(len(good) == 9, "good fixture should produce 9 criteria, got %d" % len(good))
    for crit, ok, detail, line in good:
        expect(ok, "good fixture must pass %s (%s)" % (crit, detail))

    # bad fixture 1: direct-assign revert (the bug) — W1 must be RED, rest OK
    bad1 = analyse(BAD_HOLDER, GOOD_SHEET)
    d1 = dict((c, ok) for c, ok, _, _ in bad1)
    for c, ok in d1.items():
        if c.startswith("W1"):
            expect(not ok, "bad1 %s must be RED" % c)
        else:
            expect(ok, "bad1 %s must stay OK" % c)

    # bad fixture 2: legacy inline derivation + no mapper call — W1 + W3d red
    bad2 = analyse(LEGACY_HOLDER, GOOD_SHEET)
    d2 = dict((c, ok) for c, ok, _, _ in bad2)
    expect(not d2["W1 onPageStarted publishes via fromAssetLoaderUrl(url, assetRootDir)"],
           "bad2 W1 must be RED")
    expect(d2["W3c origin host constant declared exactly once"],
           "bad2 W3c must stay OK (const declared once in this snippet)")
    expect(not d2["W3d legacy inline file.name derivation removed"],
           "bad2 W3d must be RED (file.name present)")

    # bad fixture 3: downstream rewritten — W4 must be RED, holder side OK
    bad3 = analyse(GOOD_HOLDER, BAD_SHEET)
    d3 = dict((c, ok) for c, ok, _, _ in bad3)
    for c, ok in d3.items():
        if c.startswith("W4"):
            expect(not ok, "bad3 %s must be RED" % c)
        else:
            expect(ok, "bad3 %s must stay OK" % c)

    if failures:
        print("SELFTEST FAIL:")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("selftest: good=GREEN, direct-assign revert=RED, legacy inline=RED, "
          "downstream rewrite=RED")
    return 0


def main(argv):
    if "--selftest" in argv:
        rc = selftest()
        if rc == 0:
            print("WEBVIEW_HOLDER_URL_SOURCE_SELFTEST=PASS")
        return rc

    args = [a for a in argv[1:] if a != "--selftest"]
    root = args[0] if args else "."
    paths = []
    for rel in (HOLDER_REL, SHEET_REL):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            print("FAIL required file missing: %s" % rel)
            return 1
        paths.append(p)

    holder_code = open(paths[0], encoding="utf-8").read()
    sheet_code = open(paths[1], encoding="utf-8").read()
    results = analyse(holder_code, sheet_code)

    failed = [(c, d, l) for c, ok, d, l in results if not ok]
    for crit, ok, detail, line in results:
        if ok:
            print("  [OK] %s" % crit)
        else:
            print("  [FAIL] %s%s — %s" % (crit, " (line %s)" % line if line else "", detail))

    total = len(results)
    print("Summary: %d passed, %d failed" % (total - len(failed), len(failed)))
    if failed:
        print("webview holder url source gate: RED — the single-source contract is violated")
        return 1
    print("GATE=GREEN webview_holder_url_source")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
