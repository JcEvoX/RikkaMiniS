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

deep-audit-v14 FIX-B1 (ISSUE-01 + ISSUE-02) adds two criteria groups:
  W5  mobile-mode viewport policy in toggleDesktopMode: the mobile `else`
      branch must keep `useWideViewPort` / `loadWithOverviewMode` ON — the
      exact polarity of the init defaults (:127-128). A `= false` revert in
      the function body is RED; deleting the restore is RED (the body must
      consume `mobileViewportSettings()`); the policy itself must stay
      single-sourced and pinned to `true` / `true`.
  W6  gallery window contract (ui/components/ImageGalleryViewer.kt): the
      OUTER `DisposableEffect(Unit)` (host composition, before the Dialog)
      must take its window from the HOST Activity (`view.context as? Activity`)
      — `findDialogWindow(view)` on the host view walks a parent chain with no
      DialogWindowProvider and returned null on every entry (silent no-op);
      the INNER dialog effect must keep `findDialogWindow(dialogView)` and
      apply `galleryDialogWindowFlags()` (FLAG_LAYOUT_NO_LIMITS |
      FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS — without the latter the dialog
      leaves a grey band under the status bar).

Usage: python3 webview_holder_url_source_check.py [ROOT]   (ROOT = repo root)
       python3 webview_holder_url_source_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

HOLDER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt"
SHEET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewBottomSheet.kt"
GALLERY_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/components/ImageGalleryViewer.kt"
# dv17-3 (B-03): the other three WebView preview hosts.
FULLSCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewFullscreenScreen.kt"
FILEPREVIEW_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/FilePreviewScreen.kt"
KATEX_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/markdown/KaTeXView.kt"

ANDROIDVIEW_CALL_RE = re.compile(r"\bAndroidView\s*\(")
KEY_CALL_RE = re.compile(r"\bkey\s*\(")

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


def slice_balanced_body(code, header_marker):
    """Return the brace-balanced body of the function/effect whose header line
    contains `header_marker` (first match), or None.

    Runs on comment-stripped code; string-aware (braces inside string
    literals are skipped). An unterminated block returns everything scanned —
    callers must treat a missing marker (None) as RED, not skip.
    """
    lines = code.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if header_marker in line:
            start = idx
            break
    if start is None:
        return None
    depth = 0
    opened = False
    buf = []
    quote = None
    for line in lines[start:]:
        i = 0
        n = len(line)
        while i < n:
            c = line[i]
            if quote is not None:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if c in ('"', "'"):
                quote = c
            elif c == '{':
                depth += 1
                opened = True
            elif c == '}':
                depth -= 1
            i += 1
        buf.append(line)
        if opened and depth <= 0:
            return "\n".join(buf)
    return "\n".join(buf)


def block_from(text, open_idx):
    """Balanced `{ … }` starting at [open_idx], or None when unterminated."""
    depth = 0
    quote = None
    for j in range(open_idx, len(text)):
        c = text[j]
        if quote is not None:
            if c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:j + 1]
    return None


def enclosing_heads(src, idx):
    """Heads of every `{ … }` block enclosing [idx], innermost first. The head
    is the text on the line before the brace (`if (…)`, `x.also`, `?.let`)."""
    heads = []
    depth = 0
    i = idx
    while i >= 0:
        c = src[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                j = i - 1
                while j >= 0 and src[j] in " \t\n":
                    j -= 1
                end = j + 1
                while j >= 0 and src[j] not in "\n;{}":
                    j -= 1
                heads.append(src[j + 1:end].strip())
            else:
                depth -= 1
        i -= 1
    return heads


def in_discarded_lambda(src, idx):
    """True when the expression at [idx] sits inside a `.also { … }` /
    `.apply { … }` lambda — those return their RECEIVER, so the value computed
    inside is thrown away. [wide-1024] `url.also { fromAssetLoaderUrl(url, root) }`
    is exactly how the reverse mapping got called and discarded at once."""
    for head in enclosing_heads(src, idx):
        if head.endswith(".also") or head.endswith(".apply"):
            return True
    return False


def window_after(code, marker, size):
    """Bounded text window starting at `marker` (first match) — for pinning
    expression-bodied declarations whose body has no braces to slice."""
    idx = code.find(marker)
    if idx < 0:
        return None
    return code[idx:idx + size]


def key_wrapped_blocks(code, callee="AndroidView"):
    """[(args_text, body_text)] for every `key(...) { … }` whose trailing lambda
    block calls `callee(`.

    The block lives OUTSIDE the key's paren list (`key(a, b) { … }` is a trailing
    lambda), so we cannot stop at the matching `)`: we slice the brace-balanced
    body starting at the `key(` line with the gate's own slicer, which is already
    string-aware and nesting-aware. Matching is on the CALL SHAPE throughout —
    never on a bare identifier sitting on a line (a comment or a log string that
    merely names `AndroidView` must not move the gate).
    """
    out = []
    lines = code.splitlines()
    for idx, line in enumerate(lines):
        if not KEY_CALL_RE.search(line):
            continue
        body = slice_balanced_body("\n".join(lines[idx:]), line.strip())
        if body is None:
            continue
        if not ANDROIDVIEW_CALL_RE.search(body):
            continue
        args = body.split("{", 1)[0]
        out.append((args, body))
    return out


def androidview_key_criterion(label, code, required_tokens, site):
    """Criterion tuple: every `AndroidView(` call site in `code` must sit inside a
    `key(...)` block whose key arguments carry all of `required_tokens`.

    Decision point (not a spelling): the AndroidView node is re-created exactly
    when the enclosing `key` value changes, so the key must carry the *data
    source* the factory closes over. Counting the call sites and requiring
    `wrapped == total` also fails closed when the preview is removed from the
    file altogether (0 sites ⇒ RED), which a "must contain key(" style pin
    would silently accept.
    """
    stripped = strip_comments(code)
    total = len(ANDROIDVIEW_CALL_RE.findall(stripped))
    blocks = key_wrapped_blocks(stripped)
    wrapped = sum(len(ANDROIDVIEW_CALL_RE.findall(b)) for _a, b in blocks)
    bad_args = [a.strip() for a, _b in blocks
                if not all(re.search(r"\b" + re.escape(t) + r"\b", a) for t in required_tokens)]
    ok = total >= 1 and wrapped == total and not bad_args
    detail = ("%s: %d AndroidView site(s), %d outside key(...); key(...) args %s; "
              "required key tokens %s"
              % (site, total, total - wrapped,
                 ("missing %r" % bad_args[:1]) if bad_args else "carry them",
                 list(required_tokens)))
    return (label, ok, detail, None)


def analyse(holder_code, sheet_code, gallery_code=None, filepreview_code=None,
            katex_code=None, fullscreen_code=None):
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
        # [wide-1024 W1] "calls the mapper" is not "publishes the mapped value".
        # `url.also { fromAssetLoaderUrl(url, assetRootDir) }` calls it, throws
        # the result away and publishes the raw asset-loader URL — the exact
        # three-consumer breakage this gate exists for.
        pos = rhs.find("fromAssetLoaderUrl(")
        discarded = pos >= 0 and in_discarded_lambda(rhs, pos)
        ok = (pos >= 0 and "url" in rhs and "assetRootDir" in rhs
              and "?:" in rhs and not discarded)
        detail = "RHS = %r" % rhs
        if discarded:
            detail += (" — the mapper is called inside .also/.apply, so the published "
                       "value is the receiver (the raw webview URL), not the mapped "
                       "file:// URL")
        results.append(("W1 onPageStarted publishes via fromAssetLoaderUrl(url, assetRootDir)",
                        ok,
                        detail, line_no + 1))

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

    # --- W5: mobile-mode viewport policy in toggleDesktopMode (dv14-B1) ---
    # Decision point: the mobile `else` branch must keep the wide viewport +
    # overview mode ON (exact polarity of the init defaults :127-128).
    holder_stripped = strip_comments(holder_code)
    if "fun toggleDesktopMode(" in holder_stripped:
        body = slice_balanced_body(holder_stripped, "fun toggleDesktopMode(")
        if body is None:
            results.append(("W5 toggleDesktopMode body sliceable", False,
                            "toggleDesktopMode body not found / unterminated", None))
        else:
            no_false = ("useWideViewPort = false" not in body
                        and "loadWithOverviewMode = false" not in body)
            results.append(("W5a toggleDesktopMode never turns the mobile viewport off", no_false,
                            "a `useWideViewPort = false` / `loadWithOverviewMode = false` "
                            "revert in the body is RED", None))
            n_policy = len(re.findall(r"\bfun\s+mobileViewportSettings\(", holder_stripped))
            results.append(("W5b mobileViewportSettings defined exactly once", n_policy == 1,
                            "found %d definitions" % n_policy, None))
            pin_win = window_after(holder_stripped, "fun mobileViewportSettings", 300)
            pinned = (pin_win is not None
                      and "useWideViewPort = true" in pin_win
                      and "loadWithOverviewMode = true" in pin_win)
            results.append(("W5c policy stays pinned to useWideViewPort/overview = true", pinned,
                            "policy body must keep both flags ON (window after the "
                            "definition, %s)" % ("missing" if pin_win is None else pin_win[:120]),
                            None))
            consumes = "mobileViewportSettings()" in body
            results.append(("W5d mobile else branch consumes mobileViewportSettings()", consumes,
                            "deleting the restore is RED (body must go through the "
                            "single-sourced policy)", None))
            # A negation at the consumption site flips the polarity just as
            # hard as a `false` literal did, and the policy value itself stays
            # green — this arm is what catches it (m7 mutation). Scope: the
            # two viewport fields only; `desktopMode = !desktopMode` is the
            # toggle itself and must not trip it.
            neg = re.search(r"(useWideViewPort|loadWithOverviewMode)\s*=\s*!", body)
            results.append(("W5e toggleDesktopMode assigns the policy without negation",
                            neg is None,
                            "negated assignment (%r) inverts the mobile viewport "
                            "again" % (neg.group(0) if neg else ""), None))
            # [wide-1024 W5] the mobile branch must COPY the policy's field, not
            # derive a new predicate from another one: `mobile.initialScale > 0`
            # is false (initialScale is 0) and silently turns BOTH flags off,
            # which is the polarity revert the gate exists for, spelled so that
            # `= false` never appears.
            mobile_branch = None
            m_else = re.search(r"\belse\s*\{", body)
            if m_else:
                # slice from the else's OWN brace: the `}` that closes the
                # desktop branch sits before it and would make a
                # header-marker slicer return just the header line (a silently
                # vacuous arm — measured on the DESKTOP_FALSE fixture).
                mobile_branch = block_from(body, body.find("{", m_else.start()))
            policy_local = re.search(r"val\s+([A-Za-z_]\w*)\s*=\s*mobileViewportSettings\s*\(\s*\)",
                                     body)
            recv = [re.escape(policy_local.group(1))] if policy_local else []
            recv.append(r"mobileViewportSettings\s*\(\s*\)")
            recv_re = "(?:" + "|".join(recv) + ")"
            if mobile_branch:
                for field in ("useWideViewPort", "loadWithOverviewMode"):
                    assigned = [a.strip() for a in re.findall(
                        r"\.\s*" + field + r"\s*=\s*([^\n]+)", mobile_branch)]
                    bad = [a for a in assigned
                           if not re.fullmatch(r"!?\s*\(?\s*" + recv_re
                                               + r"\s*\.\s*" + field + r"\s*\)?", a)]
                    results.append((
                        "W5f mobile branch copies the policy's own %s field" % field,
                        not bad,
                        "assigned %r -- the mobile branch must copy the policy field "
                        "(a derived predicate such as `mobile.initialScale > 0` flips "
                        "the viewport off because initialScale is 0)"
                        % (bad[0] if bad else ""), None))

    # --- W6: gallery window contract (dv14-B1) ---
    if gallery_code is not None:
        gallery_stripped = strip_comments(gallery_code)
        outer = slice_balanced_body(gallery_stripped, "DisposableEffect(Unit)")
        if outer is None:
            results.append(("W6 gallery outer DisposableEffect(Unit) sliceable", False,
                            "outer effect not found / unterminated", None))
        else:
            ok_host = "(view.context as? android.app.Activity)?.window" in outer
            results.append(("W6a gallery outer effect takes the HOST Activity window", ok_host,
                            "the window must come from view.context as Activity, "
                            "not from a DialogWindowProvider walk", None))
            ok_no_walk = "findDialogWindow(view)" not in outer
            results.append(("W6b outer effect must not call findDialogWindow(view)", ok_no_walk,
                            "findDialogWindow(view) on the host view walks a chain "
                            "with no DialogWindowProvider (always null)", None))
        # [wide-1024 W6] the window local may be correct while the controller is
        # built from something else: `(null as android.view.Window?)?.let { … }`
        # keeps every token (the cast, the constructor) and yields a null
        # controller, so immersive mode silently stops hiding the system bars.
        win_local = re.search(
            r"\bval\s+([A-Za-z_]\w*)\s*=\s*[^\n]*as\?\s+android\.app\.Activity[^\n]*",
            outer)
        # the prefix must belong to THIS binding: stop the search at any other
        # declaration, otherwise a correct `val window = …` above satisfies it
        ctl = re.search(
            r"\bval\s+([A-Za-z_]\w*)\s*=\s*"
            r"((?:(?!\n\s*(?:val|var|fun|internal|private|public)\b)[\s\S])*?)"
            r"WindowInsetsControllerCompat\s*\(", outer)
        ok_ctl = False
        ctl_detail = "controller binding not found"
        if win_local is None:
            ctl_detail = "the host-activity window is not bound to a local"
        elif ctl is None:
            ctl_detail = "no `val <controller> = …WindowInsetsControllerCompat(…)` binding"
        else:
            ctl_detail = ("`%s` is built from `%s`, not from the window local `%s`"
                          % (ctl.group(1), " ".join(ctl.group(2).split())[:70],
                             win_local.group(1)))
            ok_ctl = re.search(r"\b" + re.escape(win_local.group(1)) + r"\b",
                               ctl.group(2)) is not None
        results.append(("W6e gallery outer effect builds the controller from that window",
                        ok_ctl, ctl_detail, None))
        inner = slice_balanced_body(gallery_stripped, "DisposableEffect(dialogView)")
        if inner is None:
            results.append(("W6 gallery inner DisposableEffect(dialogView) sliceable", False,
                            "inner effect not found / unterminated", None))
        else:
            ok_flags_call = "galleryDialogWindowFlags()" in inner
            results.append(("W6c inner dialog effect applies galleryDialogWindowFlags()", ok_flags_call,
                            "the dialog window must get FLAG_LAYOUT_NO_LIMITS | "
                            "FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS", None))
        n_flags = len(re.findall(r"\bfun\s+galleryDialogWindowFlags\(", gallery_stripped))
        flags_win = window_after(gallery_stripped, "fun galleryDialogWindowFlags", 300)
        ok_flags_def = (n_flags == 1 and flags_win is not None
                        and "FLAG_LAYOUT_NO_LIMITS" in flags_win
                        and "FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS" in flags_win)
        results.append(("W6d galleryDialogWindowFlags defined once with both flags", ok_flags_def,
                        "found %d definitions; both flag constants must stay in the "
                        "composition" % n_flags, None))

    # --- W7: preview WebView hosts must key the AndroidView on its data source (dv17-3) ---
    # `AndroidView(factory = …)` runs `factory` only on first composition, or when
    # the enclosing `key(...)` value changes. Without a key the node keeps the
    # WebView it built first while its owner moves on:
    #   * sheet + fullscreen: the holder is replaceable (rememberWebViewHolder(url)
    #     rebuilds it on a URL change) while the node keeps its slot ⇒ the screen
    #     keeps showing the *destroyed* WebView of the old holder;
    #   * FilePreviewScreen / KaTeXView: `rendererEpoch` is `remember(dataSource)`,
    #     so on a data-source change it resets to 0 — a bare `key(rendererEpoch)`
    #     never changes value (0→0) and the factory keeps its stale closure.
    # The key must therefore carry the data source AND keep the renderer-death
    # rebuild (`rendererEpoch`) — that path stays live for both hosts.
    if fullscreen_code is not None:
        results.append(androidview_key_criterion(
            "W7a WebPreviewBottomSheet AndroidView is keyed on the holder",
            sheet_code, ("holder",), "WebPreviewBottomSheet.kt"))
        results.append(androidview_key_criterion(
            "W7b WebPreviewFullscreenScreen AndroidView is keyed on the holder",
            fullscreen_code, ("holder",), "WebPreviewFullscreenScreen.kt"))
    if filepreview_code is not None:
        results.append(androidview_key_criterion(
            "W7c FilePreviewScreen HtmlPreview keys AndroidView on (item.file, rendererEpoch)",
            filepreview_code, ("item.file", "rendererEpoch"), "FilePreviewScreen.kt"))
    if katex_code is not None:
        results.append(androidview_key_criterion(
            "W7d KaTeXView keys AndroidView on (cacheKey, rendererEpoch)",
            katex_code, ("cacheKey", "rendererEpoch"), "KaTeXView.kt"))
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

# ---- dv14-B1 fixtures: W5 (toggleDesktopMode viewport policy) -------------

DESKTOP_GOOD_HOLDER = GOOD_HOLDER + """
class DesktopHolder {
    fun toggleDesktopMode() {
        if (desktopMode) {
            webView.settings.useWideViewPort = true
            webView.settings.loadWithOverviewMode = true
        } else {
            val mobile = mobileViewportSettings()
            webView.settings.useWideViewPort = mobile.useWideViewPort
            webView.settings.loadWithOverviewMode = mobile.loadWithOverviewMode
            webView.setInitialScale(mobile.initialScale)
        }
    }
}
internal fun mobileViewportSettings(): MobileViewportSettings =
    MobileViewportSettings(useWideViewPort = true, loadWithOverviewMode = true, initialScale = 0)
"""

DESKTOP_FALSE_HOLDER = GOOD_HOLDER + """
class DesktopHolder {
    fun toggleDesktopMode() {
        if (desktopMode) {
            webView.settings.useWideViewPort = true
        } else {
            webView.settings.useWideViewPort = false
            webView.settings.loadWithOverviewMode = false
        }
    }
}
internal fun mobileViewportSettings(): MobileViewportSettings =
    MobileViewportSettings(useWideViewPort = true, loadWithOverviewMode = true, initialScale = 0)
"""

DESKTOP_POLICY_FLIPPED_HOLDER = GOOD_HOLDER + """
class DesktopHolder {
    fun toggleDesktopMode() {
        val mobile = mobileViewportSettings()
        webView.settings.useWideViewPort = mobile.useWideViewPort
        webView.settings.loadWithOverviewMode = mobile.loadWithOverviewMode
    }
}
internal fun mobileViewportSettings(): MobileViewportSettings =
    MobileViewportSettings(useWideViewPort = false, loadWithOverviewMode = false, initialScale = 0)
"""

DESKTOP_NO_RESTORE_HOLDER = GOOD_HOLDER + """
class DesktopHolder {
    fun toggleDesktopMode() {
        if (desktopMode) {
            webView.settings.useWideViewPort = true
        } else {
            webView.settings.userAgentString = mobileUserAgent
        }
    }
}
internal fun mobileViewportSettings(): MobileViewportSettings =
    MobileViewportSettings(useWideViewPort = true, loadWithOverviewMode = true, initialScale = 0)
"""

DESKTOP_NEGATED_HOLDER = GOOD_HOLDER + """
class DesktopHolder {
    fun toggleDesktopMode() {
        val mobile = mobileViewportSettings()
        webView.settings.useWideViewPort = !mobile.useWideViewPort
        webView.settings.loadWithOverviewMode = !mobile.loadWithOverviewMode
        webView.setInitialScale(mobile.initialScale)
    }
}
internal fun mobileViewportSettings(): MobileViewportSettings =
    MobileViewportSettings(useWideViewPort = true, loadWithOverviewMode = true, initialScale = 0)
"""

# ---- dv14-B1 fixtures: W6 (gallery window contract) ------------------------

GALLERY_GOOD = """package probe
val view = LocalView.current
DisposableEffect(Unit) {
    val window = (view.context as? android.app.Activity)?.window
    val controller = window?.let { WindowInsetsControllerCompat(it, view) }
    controller?.hide(WindowInsetsCompat.Type.systemBars())
    onDispose { }
}
Dialog {
    val dialogView = LocalView.current
    DisposableEffect(dialogView) {
        val win = findDialogWindow(dialogView)
        win?.let { w -> w.addFlags(galleryDialogWindowFlags()) }
        onDispose { }
    }
}
internal fun galleryDialogWindowFlags(): Int =
    android.view.WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
        android.view.WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS
"""

GALLERY_WALKER_REVERT = GALLERY_GOOD.replace(
    "val window = (view.context as? android.app.Activity)?.window",
    "val window = findDialogWindow(view)")

GALLERY_FLAGS_MISSING = GALLERY_GOOD.replace(
    "w.addFlags(galleryDialogWindowFlags())", "w.addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS)").replace(
    """internal fun galleryDialogWindowFlags(): Int =
    android.view.WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
        android.view.WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS
""", "")


# ---- dv17-3 fixtures: W7 (AndroidView data-source keys) --------------------

W7_GOOD_SHEET = GOOD_SHEET + """
@Composable
fun Sheet(holder: WebViewHolder) {
    Box(modifier = Modifier.fillMaxSize()) {
        key(holder) {
        AndroidView(factory = { holder.webView }, modifier = Modifier.fillMaxSize())
        }
    }
}
"""

# the reported shape: node with neither key(…) nor update
W7_BAD_SHEET = GOOD_SHEET + """
@Composable
fun Sheet(holder: WebViewHolder) {
    Box(modifier = Modifier.fillMaxSize()) {
        AndroidView(factory = { holder.webView }, modifier = Modifier.fillMaxSize())
    }
}
"""

W7_GOOD_FULLSCREEN = GOOD_SHEET + """
@Composable
fun Fullscreen(holder: WebViewHolder) {
    Box(modifier = Modifier.fillMaxSize()) {
        key(holder) {
        AndroidView(factory = { holder.webView }, modifier = Modifier.fillMaxSize())
        }
    }
}
"""

W7_BAD_FULLSCREEN = GOOD_SHEET + """
@Composable
fun Fullscreen(holder: WebViewHolder) {
    Box(modifier = Modifier.fillMaxSize()) {
        AndroidView(factory = { holder.webView }, modifier = Modifier.fillMaxSize())
    }
}
"""

W7_GOOD_PREVIEW = """@Composable
private fun HtmlPreview(item: FileItem) {
    var rendererEpoch by remember(item.file) { mutableStateOf(0) }
    key(item.file, rendererEpoch) {
    AndroidView(
        modifier = Modifier.fillMaxSize(),
        factory = { ctx ->
            WebView(ctx).apply {
                webViewClient = object : SafeWebViewClient() {
                    override fun onRendererGone(view: WebView?) {
                        rendererEpoch += 1
                    }
                }
            }
        },
    )
    }
}
"""

# the reported shape: bare rendererEpoch key (resets 0→0 on a file change)
W7_BAD_PREVIEW = W7_GOOD_PREVIEW.replace("key(item.file, rendererEpoch) {", "key(rendererEpoch) {")

# key present, but the renderer-death rebuild was dropped from it
W7_PREVIEW_NO_EPOCH = W7_GOOD_PREVIEW.replace("key(item.file, rendererEpoch) {", "key(item.file) {")

W7_GOOD_KATEX = """@Composable
private fun KaTeXRenderView(latex: String, displayMode: Boolean) {
    val cacheKey = KaTeXRendererCache.cacheKey(latex, displayMode, false)
    var rendererEpoch by remember(cacheKey) { mutableStateOf(0) }
    Box(modifier = Modifier.widthIn(min = 20.dp)) {
        key(cacheKey, rendererEpoch) {
            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        setBackgroundColor(android.graphics.Color.TRANSPARENT)
                        if (dead) rendererEpoch += 1
                    }
                },
            )
        }
    }
}
"""

W7_BAD_KATEX = W7_GOOD_KATEX.replace("key(cacheKey, rendererEpoch) {", "key(rendererEpoch) {")

W7_KATEX_NO_EPOCH = W7_GOOD_KATEX.replace("key(cacheKey, rendererEpoch) {", "key(cacheKey) {")


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

    # ---- dv14-B1: W5 fixtures ----
    # good: mobile branch keeps the viewport on via the single-sourced policy
    dgood = analyse(DESKTOP_GOOD_HOLDER, GOOD_SHEET)
    for c, ok, detail, _ in dgood:
        if c.startswith("W5"):
            expect(ok, "desktop good fixture must pass %s (%s)" % (c, detail))

    # bad: literal false revert in the else branch — W5a RED; W5b/W5c stay OK
    # (W5d is allowed RED: this fixture also drops the policy consumption,
    # which W5d legitimately catches on its own)
    dfalse = dict((c, ok) for c, ok, _, _ in analyse(DESKTOP_FALSE_HOLDER, GOOD_SHEET))
    for c, ok in dfalse.items():
        if c == "W5a toggleDesktopMode never turns the mobile viewport off":
            expect(not ok, "desktop false fixture: %s must be RED" % c)
        elif c.startswith("W5") and "W5d" not in c and not c.startswith("W5f"):
            expect(ok, "desktop false fixture: %s must stay OK" % c)

    # bad: policy values flipped to false — W5c RED, wiring still OK
    dflip = dict((c, ok) for c, ok, _, _ in analyse(DESKTOP_POLICY_FLIPPED_HOLDER, GOOD_SHEET))
    for c, ok in dflip.items():
        if c == "W5c policy stays pinned to useWideViewPort/overview = true":
            expect(not ok, "policy flipped fixture: %s must be RED" % c)
        elif c.startswith("W5"):
            expect(ok, "policy flipped fixture: %s must stay OK" % c)

    # bad: restore deleted from the else branch — W5d RED
    dnorest = dict((c, ok) for c, ok, _, _ in analyse(DESKTOP_NO_RESTORE_HOLDER, GOOD_SHEET))
    for c, ok in dnorest.items():
        if c == "W5d mobile else branch consumes mobileViewportSettings()":
            expect(not ok, "no-restore fixture: %s must be RED" % c)
        elif c.startswith("W5"):
            expect(ok, "no-restore fixture: %s must stay OK" % c)

    # plain holder (no toggleDesktopMode) must produce NO W5 criteria
    expect(not any(c.startswith("W5") for c, _, _, _ in analyse(GOOD_HOLDER, GOOD_SHEET)),
           "holder without toggleDesktopMode must not emit W5 criteria")

    # bad: policy consumption negated — only W5e RED (the policy itself is
    # still correct, so W5a/W5c/W5d must stay green: this is the arm that
    # exists precisely because nothing else catches a polarity negation)
    dneg = dict((c, ok) for c, ok, _, _ in analyse(DESKTOP_NEGATED_HOLDER, GOOD_SHEET))
    for c, ok in dneg.items():
        if c == "W5e toggleDesktopMode assigns the policy without negation":
            expect(not ok, "negated fixture: %s must be RED" % c)
        elif c.startswith("W5"):
            expect(ok, "negated fixture: %s must stay OK" % c)

    # ---- dv14-B1: W6 fixtures ----
    dgal = analyse(GOOD_HOLDER, GOOD_SHEET, GALLERY_GOOD)
    w6 = [(c, ok) for c, ok, _, _ in dgal if c.startswith("W6")]
    expect(len(w6) == 5, "gallery fixture should emit 5 W6 criteria, got %d" % len(w6))
    for c, ok in w6:
        expect(ok, "gallery good fixture must pass %s" % c)

    dwalk = dict((c, ok) for c, ok, _, _ in analyse(GOOD_HOLDER, GOOD_SHEET, GALLERY_WALKER_REVERT))
    for c, ok in dwalk.items():
        if c in ("W6a gallery outer effect takes the HOST Activity window",
                 "W6b outer effect must not call findDialogWindow(view)",
                 "W6e gallery outer effect builds the controller from that window"):
            expect(not ok, "walker revert fixture: %s must be RED" % c)
        elif c.startswith("W6"):
            expect(ok, "walker revert fixture: %s must stay OK" % c)

    dflags = dict((c, ok) for c, ok, _, _ in analyse(GOOD_HOLDER, GOOD_SHEET, GALLERY_FLAGS_MISSING))
    for c, ok in dflags.items():
        if c in ("W6c inner dialog effect applies galleryDialogWindowFlags()",
                 "W6d galleryDialogWindowFlags defined once with both flags"):
            expect(not ok, "flags missing fixture: %s must be RED" % c)
        elif c.startswith("W6"):
            expect(ok, "flags missing fixture: %s must stay OK" % c)

    # ---- dv17-3: W7 fixtures (AndroidView data-source keys) ----
    def w7(sheet_fx, preview_fx, katex_fx, fullscreen_fx):
        return dict((c, ok) for c, ok, _, _ in analyse(
            GOOD_HOLDER, sheet_fx, GALLERY_GOOD,
            filepreview_code=preview_fx, katex_code=katex_fx,
            fullscreen_code=fullscreen_fx))

    good7 = w7(W7_GOOD_SHEET, W7_GOOD_PREVIEW, W7_GOOD_KATEX, W7_GOOD_FULLSCREEN)
    w7_names = [c for c in good7 if c.startswith("W7")]
    expect(len(w7_names) == 4,
           "W7 fixture set should emit 4 criteria, got %d" % len(w7_names))
    for c in w7_names:
        expect(good7[c], "W7 good fixture must pass %s (%s)" % (c, good7[c] and "" or ""))

    # a holder without both extra code params must not emit W7 at all
    expect(not any(c.startswith("W7") for c, _, _, _ in analyse(GOOD_HOLDER, GOOD_SHEET)),
           "analyse without the preview files must not emit W7 criteria")

    # each revert reds exactly its OWN criterion (and disturbs nothing else)
    w7_cases = [
        (W7_BAD_SHEET, W7_GOOD_PREVIEW, W7_GOOD_KATEX, W7_GOOD_FULLSCREEN, "W7a"),
        (W7_GOOD_SHEET, W7_GOOD_PREVIEW, W7_GOOD_KATEX, W7_BAD_FULLSCREEN, "W7b"),
        (W7_GOOD_SHEET, W7_BAD_PREVIEW, W7_GOOD_KATEX, W7_GOOD_FULLSCREEN, "W7c"),
        (W7_GOOD_SHEET, W7_GOOD_PREVIEW, W7_BAD_KATEX, W7_GOOD_FULLSCREEN, "W7d"),
        (W7_GOOD_SHEET, W7_PREVIEW_NO_EPOCH, W7_GOOD_KATEX, W7_GOOD_FULLSCREEN, "W7c"),
        (W7_GOOD_SHEET, W7_GOOD_PREVIEW, W7_KATEX_NO_EPOCH, W7_GOOD_FULLSCREEN, "W7d"),
    ]
    for sheet_fx, preview_fx, katex_fx, fullscreen_fx, red_label in w7_cases:
        d = w7(sheet_fx, preview_fx, katex_fx, fullscreen_fx)
        reds = sorted(c for c in d if c.startswith("W7") and not d[c])
        expect(len(reds) == 1 and reds[0].startswith(red_label),
               "W7 revert fixture must red exactly %s, got %s" % (red_label, reds))
        others = sorted(c for c, ok in d.items() if not c.startswith("W7") and not ok)
        expect(not others, "W7 fixture must not disturb other criteria: %s" % others)

    # ---- wide-1024 witnesses ---------------------------------------------
    # Each fixture is the auditor's patch verbatim: the gate used to stay green
    # on it. It must now red EXACTLY the criterion that owns it.
    W1_ALSO_HOLDER = GOOD_HOLDER.replace(
        "currentUrl = fromAssetLoaderUrl(url, assetRootDir) ?: url",
        "currentUrl = url.also { fromAssetLoaderUrl(url, assetRootDir) }")
    d = dict((c, ok) for c, ok, _, _ in analyse(W1_ALSO_HOLDER, GOOD_SHEET))
    for c, ok in d.items():
        if c.startswith("W1"):
            expect(not ok, "wide-1024 W1 witness: %s must be RED" % c)
        else:
            expect(ok, "wide-1024 W1 witness must not disturb %s" % c)

    # equivalent spelling that must stay GREEN: parenthesised elvis
    paren = GOOD_HOLDER.replace(
        "currentUrl = fromAssetLoaderUrl(url, assetRootDir) ?: url",
        "currentUrl = (fromAssetLoaderUrl(url, assetRootDir) ?: url)")
    d = dict((c, ok) for c, ok, _, _ in analyse(paren, GOOD_SHEET))
    for c, ok in d.items():
        if c.startswith("W1"):
            expect(ok, "parenthesised elvis must stay GREEN on %s" % c)

    DESKTOP_DERIVED_HOLDER = DESKTOP_GOOD_HOLDER.replace(
        "webView.settings.useWideViewPort = mobile.useWideViewPort",
        "webView.settings.useWideViewPort = mobile.initialScale > 0").replace(
        "webView.settings.loadWithOverviewMode = mobile.loadWithOverviewMode",
        "webView.settings.loadWithOverviewMode = mobile.initialScale > 0")
    d = dict((c, ok) for c, ok, _, _ in analyse(DESKTOP_DERIVED_HOLDER, GOOD_SHEET))
    for c, ok in d.items():
        if c.startswith("W5f"):
            expect(not ok, "wide-1024 W5 witness: %s must be RED" % c)
        elif c.startswith("W5"):
            expect(ok, "wide-1024 W5 witness must not disturb %s" % c)

    GALLERY_NULL_CONTROLLER = GALLERY_GOOD.replace(
        "val controller = window?.let { WindowInsetsControllerCompat(it, view) }",
        "val controller = (null as android.view.Window?)?.let { "
        "WindowInsetsControllerCompat(it, view) }")
    d = dict((c, ok) for c, ok, _, _ in analyse(GOOD_HOLDER, GOOD_SHEET,
                                               GALLERY_NULL_CONTROLLER))
    for c, ok in d.items():
        if c == "W6e gallery outer effect builds the controller from that window":
            expect(not ok, "wide-1024 W6 witness: %s must be RED" % c)
        elif c.startswith("W6"):
            expect(ok, "wide-1024 W6 witness must not disturb %s" % c)

    if failures:
        print("SELFTEST FAIL:")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("selftest: good=GREEN, direct-assign revert=RED, legacy inline=RED, "
          "downstream rewrite=RED, desktop viewport false revert=RED, "
          "policy flip=RED, restore delete=RED, gallery walker revert=RED, "
          "gallery flags missing=RED, androidview key removed=RED (4 sites), "
          "androidview rendererEpoch dropped=RED, mapper discarded via .also=RED, "
          "viewport derived from initialScale=RED, controller built from null window=RED")
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
    for rel in (HOLDER_REL, SHEET_REL, GALLERY_REL, FULLSCREEN_REL,
                FILEPREVIEW_REL, KATEX_REL):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            print("FAIL required file missing: %s" % rel)
            return 1
        paths.append(p)

    holder_code = open(paths[0], encoding="utf-8").read()
    sheet_code = open(paths[1], encoding="utf-8").read()
    gallery_code = open(paths[2], encoding="utf-8").read()
    fullscreen_code = open(paths[3], encoding="utf-8").read()
    filepreview_code = open(paths[4], encoding="utf-8").read()
    katex_code = open(paths[5], encoding="utf-8").read()
    results = analyse(holder_code, sheet_code, gallery_code,
                      filepreview_code=filepreview_code, katex_code=katex_code,
                      fullscreen_code=fullscreen_code)

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
