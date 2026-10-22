#!/usr/bin/env python3
"""file_uri_parse_guard_check — deep-audit-1023 FIX-H (ISSUE-08, P1) gate.

Local file:// URLs are NOT generic parsed URIs at the consumption sites: the
host constructs them by plain concatenation (`"file://" + absolutePath`), so
`Uri.parse(url).path` REParses the string as a generic URI and re-applies
delimiter semantics that the construction side never encoded for:

    file:///data/x/report#1.html   -> .path == "/data/x/report"   (fragment cut)
    file:///data/x/v1?draft.html   -> .path == "/data/x/v1"       (query cut)

The file is exactly `report#1.html` / `v1?draft.html`; exists() is false, the
sheet's "open in browser" resolves to null and the only trace is one warning
log line (silent no-op). Markdown media hit the same cut inside
resolveMdMediaFile, whose own header comment promises `#` survives — the
Uri.parse call cancelled the contract one line below it.

The fix routes every `file://` resolution through the single URI-path exit
(ui/chat/UriPathDecode.kt decodeUriPath: decodes %XX only, `+` is a literal,
malformed `%` verbatim, never throws, no delimiter semantics), and makes
fromAssetLoaderUrl consume the asset-loader URL symmetrically with how
toAssetLoaderUrl produces it: the encoder percent-encodes the whole name
segment (`#` -> %23, `?` -> %3F), so a literal `#`/`?` in a reported asset URL
is always a fragment / query and is cut BEFORE decoding — matching the
androidx WebViewAssetLoader, which resolves the path only.

Criteria (all must hold):
  F1a WebPreviewBottomSheet.openExternalFromSheet file:// branch resolves via
      `decodeUriPath(url.removePrefix("file://"))` (slice on the branch);
  F1b no `Uri.parse(...).path` anywhere in WebPreviewBottomSheet.kt;
  F2a StreamingMarkdownText.resolveMdMediaFile file:// branch resolves via
      `decodeUriPath(stripped.removePrefix("file://"))`;
  F2b no `Uri.parse(...).path` anywhere in StreamingMarkdownText.kt;
  F3  WebViewHolder.fromAssetLoaderUrl cuts `#` and `?` before decodeUriPath
      (round-trip symmetry with toAssetLoaderUrl);
  F4  ui/** sweep: `Uri.parse(...).path` is banned outside the allowlist —
      allowlist: ui/preview/WebPreviewShortcut.kt, exactly 1 occurrence
      (same-family site OUTSIDE this branch's file set, radius discipline:
      registered in backlog §1023-H; when it is fixed, REMOVE the allowlist
      entry — a stale entry is RED so the exemption cannot outlive the debt).
      UriPathDecode.kt itself needs no exemption: it is JVM-pure and never
      parses; adding a parse there is RED like anywhere else.

Usage: python3 file_uri_parse_guard_check.py [ROOT]   (ROOT = repo root)
       python3 file_uri_parse_guard_check.py --selftest
Exit:  0 = gate green, 1 = violations (or selftest failures).
"""
import os
import re
import sys

SHEET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewBottomSheet.kt"
MD_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/StreamingMarkdownText.kt"
HOLDER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt"
UI_ROOT_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui"
# [dv22/B-06] the markdown media pipeline: one classifier, one parser, the
# gallery collector and the image fetcher.
MEDIA_MODEL_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/MarkdownBlockModel.kt"
MEDIA_PARSER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/markdown/MarkdownParser.kt"
CHAT_SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"

# relpath (repo-root-relative, forward slashes) -> max allowed occurrences
ALLOWLIST = {
    "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewShortcut.kt": 1,
}
ALLOWLIST_REASON = (
    "ui/preview/WebPreviewShortcut.kt — same-family `Uri.parse(url).path` site "
    "outside fix/da1023-file-uri-decode's file set (radius discipline); "
    "registered in backlog §1023-H. Remove this entry in the same branch "
    "that fixes it."
)

URI_PARSE_PATH_RE = re.compile(r"Uri\.parse\s*\([^()]*\)\s*\.\s*path\b", re.S)


def strip_comments(src):
    """Remove // and /* */ comments, string-literal aware (see
    webview_holder_url_source_check.strip_comments — `//` inside "file://"
    must survive)."""
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


def count_uri_parse_path(code):
    return len(URI_PARSE_PATH_RE.findall(code))


def branch_slice(code, anchor, span=40):
    """Comment-stripped code lines from the anchor line onward (span lines) —
    used to pin an expression inside a specific branch without pinning line
    numbers (they drift)."""
    lines = code.splitlines()
    for i, l in enumerate(lines):
        if anchor in l:
            return "\n".join(lines[i:i + span])
    return ""


def holder_from_slice(code):
    """Slice the fromAssetLoaderUrl body (up to the next blank-line-then-
    top-level fun, or 60 lines)."""
    lines = code.splitlines()
    for i, l in enumerate(lines):
        if "fun fromAssetLoaderUrl(" in l:
            return "\n".join(lines[i:i + 60])
    return ""


def analyse(sheet_code, md_code, holder_code, ui_codes):
    """ui_codes: {repo_root_relpath: comment_stripped_code} for EVERY .kt file
    under ui/** (the three pinned files included). Returns criteria list."""
    results = []
    sheet = strip_comments(sheet_code)
    md = strip_comments(md_code)
    holder = strip_comments(holder_code)

    # --- F1: openExternalFromSheet ------------------------------------
    branch = branch_slice(sheet, 'url.startsWith("file://")', 30)
    ok1 = "decodeUriPath(url.removePrefix(\"file://\"))" in branch
    results.append(("F1a sheet file:// branch resolves via decodeUriPath(url.removePrefix(\"file://\"))",
                    ok1, "branch slice does not contain the fixed resolver" if not ok1 else "", None))
    n1 = count_uri_parse_path(sheet)
    results.append(("F1b no Uri.parse(...).path in WebPreviewBottomSheet.kt",
                    n1 == 0, "found %d occurrence(s)" % n1, None))

    # --- F2: resolveMdMediaFile ---------------------------------------
    md_branch = branch_slice(md, 'stripped.startsWith("file://")', 20)
    ok2 = "decodeUriPath(stripped.removePrefix(\"file://\"))" in md_branch
    results.append(("F2a markdown file:// branch resolves via decodeUriPath(stripped.removePrefix(\"file://\"))",
                    ok2, "branch slice does not contain the fixed resolver" if not ok2 else "", None))
    n2 = count_uri_parse_path(md)
    results.append(("F2b no Uri.parse(...).path in StreamingMarkdownText.kt",
                    n2 == 0, "found %d occurrence(s)" % n2, None))

    # --- F3: fromAssetLoaderUrl symmetric consumption ------------------
    frm = holder_from_slice(holder)
    has_cut = ("substringBefore('#')" in frm) and ("substringBefore('?')" in frm)
    has_decode = "decodeUriPath(" in frm
    ok3 = bool(frm) and has_cut and has_decode
    detail3 = "cut=%s decode=%s" % (has_cut, has_decode)
    results.append(("F3 fromAssetLoaderUrl cuts #/? before decodeUriPath", ok3, detail3, None))

    # --- F4: ui/** sweep with allowlist --------------------------------
    for rel in sorted(ui_codes):
        code = ui_codes[rel]
        n = count_uri_parse_path(code)
        limit = ALLOWLIST.get(rel, 0)
        if rel in ALLOWLIST:
            if n != limit:
                results.append(
                    ("F4 allowlist exact for %s" % os.path.basename(rel), False,
                     ("allowlisted for %d occurrence(s) but found %d — %s"
                      % (limit, n,
                         "fix it and REMOVE the allowlist entry" if n == 0
                         else "raise the entry count consciously or fix the site")), None))
            else:
                results.append(("F4 allowlist exact for %s" % os.path.basename(rel), True,
                                 "exempted occurrence: %s" % ALLOWLIST_REASON, None))
        elif n > 0:
            results.append(
                ("F4 no Uri.parse(...).path in %s" % os.path.basename(rel), False,
                 "found %d occurrence(s) — route file:// paths through ui.chat.decodeUriPath" % n, None))

    # --- F5: one media classifier [dv22/B-06] ---------------------------
    results.extend(check_media_single_source(ui_codes))
    return results


# ------------------------------------------------- media single source [dv22] --
MEDIA_KIND_FUNS = ("mediaKindOfUrl", "mediaPathOfUrl", "mediaFileNameOfUrl")


def _block_after(code, needle):
    """The `{...}` body that follows the first occurrence of needle ('' if absent)."""
    i = code.find(needle)
    if i < 0:
        return ""
    j = code.find("{", i)
    if j < 0:
        return ""
    depth = 0
    for k in range(j, len(code)):
        ch = code[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[j:k + 1]
    return code[j:]


def _stmt_after(code, needle, span=400):
    """The statement that follows needle, cut at the next top-level fun."""
    i = code.find(needle)
    if i < 0:
        return ""
    tail = code[i:i + span]
    m = re.search(r"\n(?:internal |private )?fun ", tail[1:])
    return tail[:m.start() + 1] if m else tail


def _declares_media_set(code):
    """A hand-written media extension set: `setOf(` within reach of an mp4 token."""
    for m in re.finditer(r"setOf\(", code):
        if '"mp4"' in code[m.start():m.start() + 240]:
            return True
    return False


def check_media_single_source(ui_codes):
    """[dv22/B-06] Markdown media classification must have ONE decision point.

    Before this family, five surfaces each decided "video / audio / image" on
    their own: the block classifier, the inline splitter, the block parser (its
    own ext sets), the gallery collector (assumed image) and the image fetcher
    (assumed image). A URL form that one of them handled (`clip.mp4?t=1`) fell
    through the others, so the same message rendered as a video card in one path
    and as a broken image in another. These criteria pin the *decision points*
    (not the spelling): one extension table, one classifier, query cut before
    the extension read, `#` cut only for http(s), and absolute-path guards on
    every minis:// decode.
    """
    results = []
    classifier = ui_codes.get(MEDIA_MODEL_REL)
    parser = ui_codes.get(MEDIA_PARSER_REL)
    chat = ui_codes.get(CHAT_SCREEN_REL)
    cls, par, cht = classifier or "", parser or "", chat or ""

    # F5a: exactly one media extension table, and it lives in the classifier.
    offenders = []
    for rel in sorted(ui_codes):
        if rel == MEDIA_MODEL_REL:
            continue
        if rel in (MEDIA_PARSER_REL,) and _declares_media_set(ui_codes[rel]):
            offenders.append(os.path.basename(rel))
    has_table = ('nativeVideoExts' in cls and '"mp4"' in cls
                 and 'nativeAudioExts' in cls and '"mp3"' in cls)
    ok5a = bool(classifier) and has_table and not offenders
    results.append((
        "F5a media extension table declared once, in MarkdownBlockModel.kt",
        ok5a,
        "classifier table=%s; other media-side copies=%s" % (has_table, offenders or "none"),
        None))

    # F5b: one classifier — ext read sits on mediaFileNameOfUrl, which cuts the
    # query first; the parser owns no ext read and delegates by name.
    kind_body = _block_after(cls, "fun mediaKindOfUrl")
    # expression-bodied (no braces): use the statement, not a brace body.
    name_body = _stmt_after(cls, "fun mediaFileNameOfUrl")
    path_body = _block_after(cls, "fun mediaPathOfUrl")
    kind_ok = ("mediaFileNameOfUrl(" in kind_body and "substringAfterLast" in kind_body)
    name_ok = "mediaPathOfUrl(" in name_body
    path_ok = "substringBefore('?')" in path_body
    parser_own_ext = 'substringAfterLast(".", "")' in par or "substringAfterLast('.', "")" in par
    parser_delegates = "mediaKindOfUrl(" in par
    ok5b = (bool(kind_body) and bool(name_body) and bool(path_body)
            and kind_ok and name_ok and path_ok and not parser_own_ext and parser_delegates)
    results.append((
        "F5b ext read after query cut, parser delegates to mediaKindOfUrl",
        ok5b,
        "kind=%s name=%s path=%s parser_own_ext=%s parser_delegates=%s"
        % (kind_ok, name_ok, path_ok, parser_own_ext, parser_delegates),
        None))

    # F5c: `#` is a fragment only for http(s); in a local/minis path it may be
    # part of the file name (`foo#China.mp4`), so cutting it there mangles files.
    unguarded = []
    for m in re.finditer(r"substringBefore\('#", cls):
        window = cls[max(0, m.start() - 400):m.start()]
        if 'startsWith("http' not in window:
            unguarded.append(m.start())
    parser_hash = "substringBefore('#')" in par
    chat_chain = "substringBefore('?').substringBefore('#')" in cht
    ok5c = bool(cls) and not unguarded and not parser_hash and not chat_chain
    results.append((
        "F5c '#' cut only inside an http(s) branch; local paths keep it",
        ok5c,
        "unguarded_cls=%s parser_cut=%s chat_chain=%s"
        % (unguarded or "none", parser_hash, chat_chain),
        None))

    # F5d: `minis:///var/minis/x` decodes to an ABSOLUTE guest path; the sites
    # that prepend "/var/minis/" must keep absolute paths untouched.
    guarded = 0
    unguarded_sites = []
    for rel in sorted(ui_codes):
        for line in ui_codes[rel].splitlines():
            if '"/var/minis/$decoded"' not in line:
                continue
            if 'startsWith("/")' in line:
                guarded += 1
            else:
                unguarded_sites.append(os.path.basename(rel))
    ok5d = not unguarded_sites and guarded >= 1
    results.append((
        "F5d decoded minis:// path is absolute-guarded before use",
        ok5d,
        "guarded=%d unguarded=%s" % (guarded, unguarded_sites or "none"),
        None))
    return results


# ---------------------------------------------------------------- selftest ---
FIXED_SHEET = """package probe
fun openExternalFromSheet(context: Context, url: String) {
    val intent = if (url.startsWith("file://")) {
        runCatching {
            val raw = decodeUriPath(url.removePrefix("file://"))
            if (raw.isBlank()) return@runCatching null
            val file = java.io.File(raw)
        }
    }
}
"""

REVERTED_SHEET = """package probe
fun openExternalFromSheet(context: Context, url: String) {
    val intent = if (url.startsWith("file://")) {
        runCatching {
            val raw = Uri.parse(url).path ?: return@runCatching null
            val file = java.io.File(raw)
        }
    }
}
"""

FIXED_MD = """package probe
fun resolve(stripped: String): File? {
    val primary: File? = when {
        stripped.startsWith("minis://") -> File(decodeUriPath(stripped.removePrefix("minis://")))
        stripped.startsWith("file://") ->
            decodeUriPath(stripped.removePrefix("file://")).takeIf { it.isNotBlank() }?.let { File(it) }
        stripped.startsWith("/") -> File(stripped)
        else -> null
    }
}
"""

REVERTED_MD = """package probe
fun resolve(stripped: String): File? {
    val primary: File? = when {
        stripped.startsWith("file://") -> File(Uri.parse(stripped).path ?: return null)
        else -> null
    }
}
"""

FIXED_HOLDER = """package probe
internal fun fromAssetLoaderUrl(assetUrl: String, rootDir: String?): String? {
    val relative = decodeUriPath(
        assetUrl.removePrefix(ASSET_LOADER_PREFIX).substringBefore('#').substringBefore('?'),
    )
    return FILE_URL_PREFIX + resolved
}
"""

CUTS_REMOVED_HOLDER = """package probe
internal fun fromAssetLoaderUrl(assetUrl: String, rootDir: String?): String? {
    val relative = decodeUriPath(assetUrl.removePrefix(ASSET_LOADER_PREFIX))
    return FILE_URL_PREFIX + resolved
}
"""

CLEAN_UI_FILE = """package probe
fun host(url: String): String? = Uri.parse(url).host
fun plain(url: String) { Intent(Intent.ACTION_VIEW, Uri.parse(url)) }
"""

DIRTY_UI_FILE = """package probe
fun host(url: String): String? = Uri.parse(url).path.orEmpty()
"""

ALLOWLISTED_AS_IS = """package probe
fun abs(url: String): String = Uri.parse(url).path.orEmpty()
"""


MEDIA_CLASSIFIER_CLEAN = """package probe

internal enum class MediaKind { IMAGE, VIDEO, AUDIO }

internal val nativeVideoExts = setOf("mp4", "mov", "m4v")
internal val nativeAudioExts = setOf("mp3", "m4a")

internal fun mediaKindOfUrl(url: String): MediaKind {
    val ext = mediaFileNameOfUrl(url).substringAfterLast('.', "").lowercase()
    return when (ext) {
        in nativeVideoExts -> MediaKind.VIDEO
        in nativeAudioExts -> MediaKind.AUDIO
        else -> MediaKind.IMAGE
    }
}

internal fun mediaPathOfUrl(url: String): String {
    val noQuery = url.substringBefore('?')
    return if (noQuery.startsWith("http://") || noQuery.startsWith("https://")) {
        noQuery.substringBefore('#')
    } else {
        noQuery
    }
}

internal fun mediaFileNameOfUrl(url: String): String =
    mediaPathOfUrl(url).substringAfterLast('/')
"""

MEDIA_PARSER_CLEAN = """package probe

internal fun mediaBlockFor(alt: String, url: String) = when (mediaKindOfUrl(url)) {
    MediaKind.VIDEO -> "video"
    MediaKind.AUDIO -> "audio"
    MediaKind.IMAGE -> "image"
}
"""

CHAT_SCREEN_CLEAN = """package probe

internal fun collectImages(src: String): List<String> {
    val path = mediaPathOfUrl(src)
    if (mediaKindOfUrl(src) != MediaKind.IMAGE) return emptyList()
    return listOf(mediaFileNameOfUrl(src))
}
"""

IMAGE_FETCHER_CLEAN = """package probe

internal fun resolve(decoded: String): java.io.File {
    val linuxPath = if (decoded.startsWith("/")) decoded else "/var/minis/$decoded"
    return java.io.File(linuxPath)
}
"""

BREAKER_CLASSIFIER_HASH = MEDIA_CLASSIFIER_CLEAN.replace(
    "    return if (noQuery.startsWith(\"http://\") || noQuery.startsWith(\"https://\")) {\n"
    "        noQuery.substringBefore('#')",
    "    return if (true) {\n"
    "        noQuery.substringBefore('#')")

BREAKER_PARSER_SET = MEDIA_PARSER_CLEAN.replace(
    "internal fun mediaBlockFor",
    "private val parserExts = setOf(\"mp4\", \"mov\")\n\ninternal fun mediaBlockFor")

BREAKER_PARSER_EXT = MEDIA_PARSER_CLEAN.replace(
    "when (mediaKindOfUrl(url))",
    "when (url.substringAfterLast('.', \"\"))")

BREAKER_CHAT_CHAIN = CHAT_SCREEN_CLEAN.replace(
    "val path = mediaPathOfUrl(src)",
    "val path = src.substringBefore('?').substringBefore('#')")

BREAKER_FETCHER_UNGURARDED = IMAGE_FETCHER_CLEAN.replace(
    'val linuxPath = if (decoded.startsWith("/")) decoded else "/var/minis/$decoded"',
    'val linuxPath = "/var/minis/$decoded"')


def selftest():
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def dict_of(extra=None):
        ui = {
            "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewBottomSheet.kt": FIXED_SHEET,
            "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/StreamingMarkdownText.kt": FIXED_MD,
            "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt": FIXED_HOLDER,
            "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewShortcut.kt": ALLOWLISTED_AS_IS,
            "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/Other.kt": CLEAN_UI_FILE,
            MEDIA_MODEL_REL: MEDIA_CLASSIFIER_CLEAN,
            MEDIA_PARSER_REL: MEDIA_PARSER_CLEAN,
            CHAT_SCREEN_REL: CHAT_SCREEN_CLEAN,
            "src/android/app/src/main/java/com/rikkaminis/app/ui/MinisImageFetcher.kt": IMAGE_FETCHER_CLEAN,
        }
        if extra:
            ui.update(extra)
        return ui

    # all-green tree
    good = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of())
    good_map = {c: ok for c, ok, _, _ in good}
    for c, ok in good_map.items():
        expect(ok, "good tree must pass %s" % c)
    expect("F4 no Uri.parse(...).path in Other.kt" not in good_map,
           "a clean ui file must not emit a criterion (only offenders do); got %s" % sorted(good_map))

    # mutation 1: sheet reverted to Uri.parse().path -> F1a+F1b red, rest green
    m1 = analyse(REVERTED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewBottomSheet.kt": REVERTED_SHEET}))
    d1 = {c: ok for c, ok, _, _ in m1}
    expect(not d1["F1a sheet file:// branch resolves via decodeUriPath(url.removePrefix(\"file://\"))"],
           "mut1 F1a must be RED")
    expect(not d1["F1b no Uri.parse(...).path in WebPreviewBottomSheet.kt"],
           "mut1 F1b must be RED")
    expect(d1["F2a markdown file:// branch resolves via decodeUriPath(stripped.removePrefix(\"file://\"))"],
           "mut1 F2a must stay GREEN (precision)")

    # mutation 2: markdown reverted -> F2a+F2b red, rest green
    m2 = analyse(FIXED_SHEET, REVERTED_MD, FIXED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/StreamingMarkdownText.kt": REVERTED_MD}))
    d2 = {c: ok for c, ok, _, _ in m2}
    expect(not d2["F2a markdown file:// branch resolves via decodeUriPath(stripped.removePrefix(\"file://\"))"],
           "mut2 F2a must be RED")
    expect(not d2["F2b no Uri.parse(...).path in StreamingMarkdownText.kt"],
           "mut2 F2b must be RED")
    expect(d2["F1a sheet file:// branch resolves via decodeUriPath(url.removePrefix(\"file://\"))"],
           "mut2 F1a must stay GREEN (precision)")

    # mutation 3: fromAssetLoaderUrl loses the delimiter cuts -> F3 red only
    m3 = analyse(FIXED_SHEET, FIXED_MD, CUTS_REMOVED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt": CUTS_REMOVED_HOLDER}))
    d3 = {c: ok for c, ok, _, _ in m3}
    expect(not d3["F3 fromAssetLoaderUrl cuts #/? before decodeUriPath"],
           "mut3 F3 must be RED")
    expect(d3["F1a sheet file:// branch resolves via decodeUriPath(url.removePrefix(\"file://\"))"],
           "mut3 F1a must stay GREEN (precision)")

    # mutation 4: new Uri.parse().path in an arbitrary ui file -> sweep red
    m4 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/Other.kt": DIRTY_UI_FILE}))
    d4 = {c: ok for c, ok, _, _ in m4}
    expect(not d4["F4 no Uri.parse(...).path in Other.kt"],
           "mut4 sweep must be RED for a new offender")

    # mutation 5: allowlisted file fixed to zero occurrences -> stale red
    m5 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebPreviewShortcut.kt": CLEAN_UI_FILE}))
    d5 = {c: ok for c, ok, _, _ in m5}
    expect(not d5["F4 allowlist exact for WebPreviewShortcut.kt"],
           "mut5 stale allowlist entry must be RED (remove the entry when fixed)")

    # precision: Uri.parse(...).host / ACTION_VIEW Uri.parse(url) must not trip
    n_clean = count_uri_parse_path(strip_comments(CLEAN_UI_FILE))
    expect(n_clean == 0, "host/plain Uri.parse uses must not count as violations (got %d)" % n_clean)

    # mutation 6: classifier cuts '#' unconditionally -> F5c red only
    m6 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({MEDIA_MODEL_REL: BREAKER_CLASSIFIER_HASH}))
    d6 = {c: ok for c, ok, _, _ in m6}
    expect(not d6["F5c '#' cut only inside an http(s) branch; local paths keep it"],
           "mut6 F5c must be RED when '#' is cut unconditionally")
    expect(d6["F5a media extension table declared once, in MarkdownBlockModel.kt"],
           "mut6 F5a must stay GREEN (precision)")

    # mutation 7: parser keeps its own extension set -> F5a red only
    m7 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({MEDIA_PARSER_REL: BREAKER_PARSER_SET}))
    d7 = {c: ok for c, ok, _, _ in m7}
    expect(not d7["F5a media extension table declared once, in MarkdownBlockModel.kt"],
           "mut7 F5a must be RED when the parser declares its own set")
    expect(d7["F5c '#' cut only inside an http(s) branch; local paths keep it"],
           "mut7 F5c must stay GREEN (precision)")

    # mutation 8: parser reads the extension itself -> F5b red
    m8 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({MEDIA_PARSER_REL: BREAKER_PARSER_EXT}))
    d8 = {c: ok for c, ok, _, _ in m8}
    expect(not d8["F5b ext read after query cut, parser delegates to mediaKindOfUrl"],
           "mut8 F5b must be RED when the parser owns an ext read")

    # mutation 9: gallery re-grows the query+fragment chain -> F5c red
    m9 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({CHAT_SCREEN_REL: BREAKER_CHAT_CHAIN}))
    d9 = {c: ok for c, ok, _, _ in m9}
    expect(not d9["F5c '#' cut only inside an http(s) branch; local paths keep it"],
           "mut9 F5c must be RED for the chat-side query+fragment chain")

    # mutation 10: fetcher prepends /var/minis/ to an absolute path -> F5d red
    m10 = analyse(FIXED_SHEET, FIXED_MD, FIXED_HOLDER, dict_of({
        "src/android/app/src/main/java/com/rikkaminis/app/ui/MinisImageFetcher.kt": BREAKER_FETCHER_UNGURARDED}))
    d10 = {c: ok for c, ok, _, _ in m10}
    expect(not d10["F5d decoded minis:// path is absolute-guarded before use"],
           "mut10 F5d must be RED when the absolute-path guard is dropped")

    # precision: the classifier's mediaBlockFrom must not be judged as a set copy
    expect(count_uri_parse_path(MEDIA_CLASSIFIER_CLEAN) == 0,
           "fixture precision: the clean classifier parses no Uri")

    if failures:
        print("SELFTEST FAIL:")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("selftest: fixed-tree=GREEN, sheet-revert=RED, md-revert=RED, "
          "holder-cut-removed=RED, new-offender=RED, stale-allowlist=RED, "
          "host-precision=GREEN, media-hash-cut=RED, media-set-fork=RED, "
          "media-parser-ext=RED, media-chat-chain=RED, media-abs-guard=RED")
    return 0


def main(argv):
    if "--selftest" in argv:
        rc = selftest()
        if rc == 0:
            print("FILE_URI_PARSE_GUARD_SELFTEST=PASS")
        return rc

    args = [a for a in argv[1:] if a != "--selftest"]
    root = args[0] if args else "."

    sheet_p = os.path.join(root, SHEET_REL)
    md_p = os.path.join(root, MD_REL)
    holder_p = os.path.join(root, HOLDER_REL)
    ui_root = os.path.join(root, UI_ROOT_REL)
    for p in (sheet_p, md_p, holder_p):
        if not os.path.isfile(p):
            print("FAIL required file missing: %s" % p)
            return 1
    if not os.path.isdir(ui_root):
        print("FAIL ui root missing: %s" % ui_root)
        return 1

    sheet_code = open(sheet_p, encoding="utf-8").read()
    md_code = open(md_p, encoding="utf-8").read()
    holder_code = open(holder_p, encoding="utf-8").read()

    ui_codes = {}
    for dirpath, _dirnames, filenames in os.walk(ui_root):
        for fn in filenames:
            if not fn.endswith(".kt"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ui_codes[rel] = strip_comments(open(full, encoding="utf-8").read())

    results = analyse(sheet_code, md_code, holder_code, ui_codes)
    failed = [r for r in results if not r[1]]
    for crit, ok, detail, line in results:
        if ok:
            print("  [OK] %s%s" % (crit, " — %s" % detail if detail else ""))
        else:
            print("  [FAIL] %s%s — %s" % (crit, " (line %s)" % line if line else "", detail))
    print("Summary: %d passed, %d failed" % (len(results) - len(failed), len(failed)))
    print("swept %d ui/** .kt file(s) under %s" % (len(ui_codes), UI_ROOT_REL))
    if failed:
        print("file uri parse guard: RED — Uri.parse(...).path on local file:// "
              "paths truncates #/? and silently no-ops; resolve via ui.chat.decodeUriPath")
        return 1
    print("GATE=GREEN file_uri_parse_guard")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
