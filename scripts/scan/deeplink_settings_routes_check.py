#!/usr/bin/env python3
"""da1020-F gate: deep-link settings routes + route-segment encoding.

Usage (repo root, positional arg supported):
    python3 scripts/scan/deeplink_settings_routes_check.py [ROOT]

PASS  = exit 0, prints DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS after --selftest
FAIL  = exit 1, prints violating 文件:行

Judged invariants:
  1. The settings-path whitelist covers the EMITTER list (minis://settings/
     links the app itself emits, incl. the MCP OAuth round-trip emitted by
     minis-mcp-cli transport/http.py).
  2. Every parameterised Routes segment builder encodes through the single
     helper (seg()) — a raw id would hand nav.navigate a malformed route
     (uncaught IllegalArgumentException).
  3. The unknown-path fallback to Settings home is still present
     (deliberate design — must not regress into an error).
  4. MainActivity's deep-link navigate has a runCatching backstop.
  5. [da1021-C] AppNavigation's cold-start deep-link navigate keeps its
     runCatching backstop.
  6. [da1024-E] No composable block decodes a route argument itself: the Nav
     framework %-decodes each path segment exactly once
     (NavDeepLink.getMatchingPathArguments -> Uri.decode), so a second decode
     rewrites ids carrying a literal '%'. Uri.encode stays legal (builders).
     Scope = the composable blocks, which is where route args are read; a
     decode added elsewhere in this file is out of scope by design (see the
     branch report's negative decisions).
  7. [da1024-E] The MODEL_ENTRY_DETAIL block still reads both args via
     getString and keeps the `?: return@composable` fail-closed guard.
  8. [da1028-C] EnvironmentVariablesScreen must OBSERVE its pending deep-link
     create request: `pendingEnvVarCreate` read through `collectAsState()`,
     and no `LaunchedEffect(Unit)` that consumes it (a one-shot effect never
     re-runs, so a warm-start deep link into an already-composed screen is
     dropped). EnvVarFormSheet's four field shadows (`keyText`, `valueText`,
     `noteText`, `groupText`) must each be keyed on what they are seeded from
     — keyless keeps the FIRST entry's text after `editEntry` switches.
  9. [da1028-C] ChatScreen must observe `pendingChatAction` the same way
     (`collectAsState()`; no `.value` snapshot). Regression anchor: the
     neighbouring `pendingHtmlPreview` subscription must stay reactive — that
     is the shape this file already uses and nothing may "simplify" it back.
  10. Anchors missing in either file = RED (a rename must not retire 8/9).
  11. [da1029-A] AppNavigation's cold-start `when (initialDeepLink)` dispatch
      names EVERY `DeepLinkAction` subclass and has no catch-all `else ->`,
      and its `OpenAlarmList` branch launches
      `AlarmClock.ACTION_SHOW_ALARMS`. `minis://views/alarm` used to be a
      silent cold-start no-op because 4 of the 11 subclasses fell into the
      old `else -> {}`. The neighbouring `quickActionStart` resolver
      (`else -> null`) is deliberately out of scope — the criterion selects
      the dispatch block by requiring its dispatch-only OpenTerminal case.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Emitter list: minis://settings/<head> links the app itself emits.
# Sources (extracted 2026-10-17 from main 942b0111d):
#   - src/android/app/src/main/assets/default_mount/usr/local/lib/minis-mcp-cli/transport/http.py:140
#       "[Authorize](minis://settings/mcp-servers/%s)"
#   - Kotlin prompt strings (ChatPromptAndTools.kt / ConfigBridge.kt /
#     EnvVarsCollection.kt / ProvidersCollection.kt):
#       minis://settings/{permissions,environments,memory}
# Re-extract with:
#   find src -type f \( -name '*.kt' -o -name '*.py' \) | xargs grep -rn "minis://settings"
EMITTER_HEADS = ["mcp-servers", "permissions", "environments", "memory"]

REL_DLH = "src/android/app/src/main/java/com/rikkaminis/app/deeplink/DeepLinkHandler.kt"
REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
REL_MAIN = "src/android/app/src/main/java/com/rikkaminis/app/MainActivity.kt"
REL_HTTP_PY = "src/android/app/src/main/assets/default_mount/usr/local/lib/minis-mcp-cli/transport/http.py"
# [da1028-C] reactive deep-link consumers
REL_ENV_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/EnvironmentVariablesScreen.kt"
REL_CHAT_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"
# the four shadow copies EnvVarFormSheet seeds from its entry
ENV_FORM_FIELDS = ("keyText", "valueText", "noteText", "groupText")

def launch_effect_blocks(code, arg):
    """[(lineno, block_text)] for every `LaunchedEffect(<arg>) { ... }`.

    A block ends at the first following `}` sitting at the opener's own
    indentation (same rule as composable_blocks).
    """
    blocks = []
    indexed = code_lines(code)
    pat = re.compile(r"LaunchedEffect\s*\(\s*" + re.escape(arg) + r"\s*\)")
    i = 0
    while i < len(indexed):
        lineno, line = indexed[i]
        if not pat.search(line):
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        j = i + 1
        while j < len(indexed):
            text = indexed[j][1]
            if text.strip() == "}" and (len(text) - len(text.lstrip())) == indent:
                break
            j += 1
        blocks.append((lineno, "\n".join(t for _, t in indexed[i:j + 1])))
        i = j + 1
    return blocks

# Parameterised Routes segment builders that MUST encode their id.
# [da1021-C] Extended to ALL 17 path-segment builders (the 6 da1020-F ones +
# the 10 that were still raw-interpolated + the new mirrorCategoryDetail).
# Excluded on purpose: fileBrowser / terminal / filePreview encode through
# their own local `enc` helpers (same android.net.Uri.encode semantics, no
# seg() dependency).
SEG_BUILDERS = [
    "providerDetail", "providerConnection", "modelGroupDetail",
    "addModelsToGroup", "addCustomModel", "modelEntryDetail",
    "scheduledTaskEdit", "skillDetail", "skillFile", "chatFiles",
    "mountedFoldersDetail", "sharedFoldersDetail", "logDetail",
    "sessionStorageDetail", "memoryFileEdit", "chat",
    "mirrorCategoryDetail",
]

VIOLATIONS = []


def code_only(src: str) -> str:
    """Strip comment lines (// line comments, /* */ and KDoc blocks) so a
    gate key living only in a comment cannot fake a pass (COMMON §4)."""
    out = []
    in_block = False
    for line in src.split("\n"):
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("*") or s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def fail(path, msg):
    VIOLATIONS.append(f"{path}: {msg}")

# [wide-1024 12-misc-b] Form-aware helpers. The criteria below used to ask
# "does the file contain this substring / does the block mention seg() once";
# every one of the four witnesses in this branch keeps the substring and drops
# the EFFECT (a raw sibling id, a constant LaunchedEffect key, a constant
# remember key, a constructed-but-never-launched Intent). Judging a spelling
# needs a shape-equivalence class; judging an effect needs the AST-level
# neighbours, so these helpers read the expression rather than the text.
KEYWORDS = {
    "true", "false", "null", "this", "it", "super", "return", "if", "else",
    "val", "var", "by", "fun", "object", "class", "when", "for", "while",
    "in", "is", "as", "try", "catch", "throw", "Unit", "String", "Boolean",
    "Int", "Long", "Float", "Double", "Any", "Nothing",
}

def string_literals(text):
    """[(start, end)] of every "…" literal; comments are skipped.

    Template strings count as literals — criterion 2 reads their `$…` holes, so
    the spans are needed both to blank and to inspect them.
    """
    spans = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c != '"':
            i += 1
            continue
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"' or text[j] == "\n":
                j += 1
                break
            j += 1
        spans.append((i, j))
        i = j
    return spans

def mask_literals(text):
    """Same length with literals blanked: indices stay valid for brace matching
    (a `{` inside a string must not unbalance the walk)."""
    chars = list(text)
    for a, b in string_literals(text):
        for k in range(a, min(b, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)

def ident_roots(expr):
    """Root identifiers an expression READS (`editEntry?.key` -> editEntry)."""
    blank = mask_literals(expr)
    out = set()
    for m in re.finditer(r"[A-Za-z_]\w*", blank):
        name = m.group(0)
        if blank[: m.start()].rstrip().endswith("."):
            continue                      # member access, not a root
        if blank[m.end():].lstrip().startswith("("):
            continue                      # call target
        if name in KEYWORDS:
            continue
        out.add(name)
    return out

def paren_text(text, open_idx):
    """Text inside the balanced (…) starting at open_idx, or None."""
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1: i]
        i += 1
    return None

def brace_text(text, open_idx):
    """Text inside the balanced {…} at open_idx, or None (callers fail-closed).
    Pass a mask_literals() copy so braces inside strings cannot unbalance it."""
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1: i]
        i += 1
    return None

def path_interpolations(lit):
    """The `$…` holes of a template literal, PATH part only (before the first
    `?` — query args are not path segments and have their own encoders)."""
    body = lit[1:-1] if lit.endswith('"') else lit[1:]
    q = body.find("?")
    if q >= 0:
        body = body[:q]
    out = []
    i = 0
    while i < len(body):
        if body[i] != "$":
            i += 1
            continue
        if i + 1 < len(body) and body[i + 1] == "{":
            depth, j = 0, i + 2
            while j < len(body):
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    if depth == 0:
                        break
                    depth -= 1
                j += 1
            out.append(body[i + 2: j])
            i = j + 1
            continue
        m = re.match(r"[A-Za-z_]\w*", body[i + 1:])
        if m:
            out.append(m.group(0))
            i += 1 + len(m.group(0))
            continue
        i += 1
    return out

def string_params(sig):
    """Builder parameters declared with type String / String? — exactly the
    values that carry an id and must therefore be encoded."""
    return {m.group(1) for m in re.finditer(r"(\w+)\s*:\s*String\b", sig)}

def launched_effects(code):
    """[(lineno, key_expr, block_body)] for every `LaunchedEffect(<key>) { … }`.

    The criterion pins the KEY, so the key is returned verbatim: `Unit`, `true`
    and `0` are the same defect in three spellings — what matters is whether
    the key READS the state the effect consumes.
    """
    masked = mask_literals(code)
    out = []
    for m in re.finditer(r"LaunchedEffect\s*\(", masked):
        arg = paren_text(masked, m.end() - 1)
        if arg is None:
            continue                      # unbalanced -> caller fail-closed
        close = m.end() + len(arg) + 1
        brace = masked.find("{", close)
        if brace < 0:
            continue
        body = brace_text(masked, brace)
        if body is None:
            continue
        out.append((masked[: m.start()].count("\n") + 1, arg, body))
    return out

def remember_decl(env, field):
    """(keys, initializer) of `var <field> by remember(<keys>) { … }`, else None."""
    masked = mask_literals(env)
    m = re.search(rf"var\s+{field}\s+by\s+remember\s*\(", masked)
    if not m:
        return None
    keys = paren_text(masked, m.end() - 1)
    if keys is None:
        return None
    brace = masked.find("{", m.end() + len(keys) + 1)
    if brace < 0:
        return None
    init = brace_text(masked, brace)
    if init is None:
        return None
    return keys, init

def case_block(text, case_re):
    """Body of the `is … -> { … }` branch matching case_re, or None."""
    masked = mask_literals(text)
    m = re.search(case_re, masked)
    if not m:
        return None
    brace = masked.find("{", m.end())
    if brace < 0:
        return None
    body = brace_text(masked, brace)
    return None if body is None else body

# [da1024-E] A second decode of a route argument. Builder-side Uri.encode is
# NOT covered (the nav graph must keep encoding when it builds a route).
DECODE_RE = re.compile(r"(?:android\.net\.|java\.net\.)?(?:Uri|URLDecoder)\.decode\s*\(")

# [da1029-A] Every DeepLinkAction subclass the cold-start dispatcher must name.
# Written out literally on purpose: deriving it from the source would make the
# criterion vacuous (the file could silently drop a branch and the expectation
# would follow it down).
COLD_START_ACTION_CASES = [
    "OpenShare", "OpenAlarmList", "OpenTerminal",
    "CreateEnvironmentVariable", "OpenPermissionSettings", "OpenSession",
    "NewChat", "NewCameraChat", "OpenSettingsScreen",
    "OpenHtmlPreview", "Unknown",
]

def when_blocks(code: str, header: str) -> "list[str]":
    """Brace-bounded text of every `when (<header>) { … }` occurrence.

    String literals are skipped while walking, so a `{`/`}` inside one cannot
    unbalance the match. Returns [] when the header is absent — callers treat
    that as a failure (fail-closed), never as a skip.
    """
    blocks = []
    i = 0
    n = len(code)
    while True:
        idx = code.find(header, i)
        if idx == -1:
            return blocks
        brace = code.find("{", idx)
        if brace == -1:
            return blocks
        depth = 0
        j = brace
        while j < n:
            c = code[j]
            if c == '"':
                if code.startswith('"""', j):
                    k = code.find('"""', j + 3)
                    j = n if k == -1 else k + 3
                    continue
                k = j + 1
                while k < n:
                    if code[k] == "\\":
                        k += 2
                        continue
                    if code[k] == '"' or code[k] == "\n":
                        k += 1
                        break
                    k += 1
                j = k
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(code[idx:j + 1])
                    break
            j += 1
        i = brace + 1

def is_comment_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")

def code_lines(src: str):
    """(1-based lineno, text) for every non-comment line.

    code_only() drops comment lines, so it shifts line numbers; the criteria
    below report positions a human can grep, so they walk the raw lines and
    skip comments themselves.
    """
    return [(i + 1, l) for i, l in enumerate(src.split("\n")) if not is_comment_line(l)]

def composable_blocks(indexed):
    """[(start_lineno, [(lineno, text), ...])] for every `composable(...) { }`.

    A block ends at the first following `}` that sits at the opener's own
    indentation, so the scan cannot bleed into the next composable's body.
    """
    blocks = []
    i = 0
    while i < len(indexed):
        lineno, line = indexed[i]
        if line.strip().startswith("composable("):
            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(indexed):
                text = indexed[j][1]
                if text.strip() == "}" and (len(text) - len(text.lstrip())) == indent:
                    break
                j += 1
            blocks.append((lineno, indexed[i:j + 1]))
            i = j + 1
        else:
            i += 1
    return blocks


def read(root: Path, rel: str) -> str:
    p = root / rel
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def check(root: Path) -> None:
    dlh = code_only(read(root, REL_DLH))
    nav_raw = read(root, REL_NAV)
    nav = code_only(nav_raw)
    main = code_only(read(root, REL_MAIN))
    if not dlh:
        fail(REL_DLH, "missing source file")
        return
    if not nav:
        fail(REL_NAV, "missing source file")
    if not main:
        fail(REL_MAIN, "missing source file")

    # --- 1. emitter whitelist coverage ---
    for head in EMITTER_HEADS:
        if head not in dlh:
            fail(REL_DLH, f"emitter head '{head}' not covered by the settings whitelist")
    # live cross-check against the actual Python emitter (SKIP-able: the gate
    # must not depend on asset directory structure at runtime)
    http_py = read(root, REL_HTTP_PY)
    if http_py:
        emitted = sorted(set(re.findall(r"minis://settings/([a-zA-Z0-9_\-]+)", http_py)))
        for head in emitted:
            if head not in dlh:
                fail(REL_HTTP_PY, f"emitted head '{head}' not covered by the settings whitelist")
    else:
        print(f"SKIP: emitter cross-check — {REL_HTTP_PY} not found (hardcoded list still enforced)")

    # --- 2. segment builders encode through the single helper ---
    # [wide-1024 12-misc-b] The old test was `count("seg(") == 0`: a builder
    # with TWO ids passed as long as ONE of them was encoded, so
    # `"model_entry/${seg(instanceId)}/$entryId"` — the composite entryId that
    # is the whole reason this criterion exists — sailed through. Judge the
    # interpolation, not the presence of the helper: every `$…` hole in a path
    # segment must be carried by seg(), unless its root identifier is not a
    # String parameter (a Boolean flag / a pre-encoded local / a `?` query arg).
    for name in SEG_BUILDERS:
        m = re.search(rf"fun {name}\(", nav)
        if not m:
            fail(REL_NAV, f"Routes.{name} builder not found")
            continue
        # definition body = from `fun name(` up to the next member boundary
        nxt = re.search(r"\n    fun |\n\}", nav[m.end():])
        body_end = m.end() + nxt.start() if nxt else len(nav)
        defn = nav[m.start():body_end]
        if defn.count("seg(") == 0:
            fail(REL_NAV, f"Routes.{name} does not encode through seg() (raw id -> malformed route)")
            continue
        eq = defn.find("=")
        params = string_params(defn[:eq] if eq > 0 else defn)
        raw = []
        for start, end in string_literals(defn):
            for expr in path_interpolations(nav[m.start() + start: m.start() + end]):
                hole = expr.strip()
                if hole.startswith("seg(") and hole.endswith(")"):
                    continue
                base = re.split(r"[?.\s]", hole)[0]
                if base in params:
                    raw.append(hole)
        if raw:
            fail(REL_NAV,
                 f"Routes.{name} interpolates raw id(s) {sorted(set(raw))} into a path "
                 "segment — every id hole must ride seg(...); one encoded sibling no "
                 "longer satisfies the criterion")

    # --- 3. fallback still present (deliberate design) ---
    if "else -> SettingsDeepLink.Route(Routes.SETTINGS)" not in dlh:
        fail(REL_DLH, "unknown-path fallback to Routes.SETTINGS is missing (deliberate design must not regress)")

    # --- 4. navigate backstop in MainActivity ---
    if "runCatching { nav.navigate(action.route) }" not in main:
        fail(REL_MAIN, "OpenSettingsScreen navigate lacks the runCatching backstop")

    # --- 5. [da1021-C] cold-start deep-link backstop in AppNavigation ---
    # The cold-start LaunchedEffect(initialDeepLink) branch runs after
    # awaitResumed(), so bare navigate() is safe there (da1020-F reasoning);
    # the runCatching is the never-crash backstop mirroring MainActivity.
    if "runCatching { navController.navigate(initialDeepLink.route) }" not in nav:
        fail(REL_NAV, "cold-start OpenSettingsScreen deep link lacks the runCatching backstop")

    # --- 6. [da1024-E] no second decode of a route argument ---
    # The Nav framework %-decodes every route path segment exactly once
    # (navigation-common 2.8.5 NavDeepLink.getMatchingPathArguments ->
    # `Uri.decode(matcher.group(index + 1))`). Anything the nav graph decodes
    # itself is therefore a SECOND decode: it rewrites an id carrying a literal
    # '%' ("100%25" -> "100%") and turns a bare '%' into U+FFFD — on Android
    # 13+ Uri.decode substitutes the replacement character instead of throwing
    # (android.net.UriCodec.decode(..., throwOnFailure = false)) — so the
    # destination's lookup misses and the screen pops straight back.
    for start, block in composable_blocks(code_lines(nav_raw)):
        for lineno, text in block:
            if DECODE_RE.search(text):
                fail(REL_NAV,
                     f"composable at line {start} decodes a route argument "
                     f"(line {lineno}): the Nav framework already "
                     f"%-decodes each path segment once")

    # --- 7. [da1024-E] MODEL_ENTRY_DETAIL reads both args, fail-closed ---
    entry_blocks = [bl for _, bl in composable_blocks(code_lines(nav_raw))
                    if any("Routes.MODEL_ENTRY_DETAIL" in t for _, t in bl)]
    if not entry_blocks:
        fail(REL_NAV, "MODEL_ENTRY_DETAIL composable block not found")
    else:
        blk = "\n".join(t for _, t in entry_blocks[0])
        for arg in ("instanceId", "entryId"):
            if f'getString("{arg}")' not in blk:
                fail(REL_NAV, f"MODEL_ENTRY_DETAIL does not read '{arg}' via getString")
        if blk.count("?: return@composable") < 2:
            fail(REL_NAV,
                 "MODEL_ENTRY_DETAIL lost its `?: return@composable` fail-closed guard")

    # --- 8. [da1028-C] EnvironmentVariablesScreen observes its deep link ----
    # The deep-link create request is a StateFlow. A `LaunchedEffect(Unit)` +
    # `.value` read fires only on first composition, so a request delivered to
    # a screen that is already up (warm start — the Activity is not recreated)
    # is silently dropped. The screen must subscribe to the flow instead.
    env = code_only(read(root, REL_ENV_SCREEN))
    if not env:
        fail(REL_ENV_SCREEN,
             "missing source file (fail-closed: this file hosts criteria 8/10)")
    else:
        if not re.search(r"pendingEnvVarCreate\s*\.\s*collectAsState\s*\(", env):
            fail(REL_ENV_SCREEN,
                 "pendingEnvVarCreate is not observed through collectAsState() — the "
                 "screen cannot see a request that arrives after first composition")
        if not re.search(r"\bconsumePendingEnvVarCreate\s*\(", env):
            fail(REL_ENV_SCREEN,
                 "consumePendingEnvVarCreate() call not found (fail-closed: criterion 8 "
                 "pins the one-shot-vs-reactive shape, re-derive it if the consume path "
                 "was renamed)")
        else:
            # [wide-1024 12-misc-b] Judge the KEY, not the spelling. The old
            # check only rejected `LaunchedEffect(Unit)` — `LaunchedEffect(true)`
            # / `(0)` are the same defect (a constant key never re-runs, so a
            # request that lands after the first composition is dropped) and
            # passed. What matters is whether the key READS the observed state.
            collected = re.findall(
                r"(\w+)\s+by\s+[\w.]*pendingEnvVarCreate\s*\.\s*collectAsState\s*\(", env)
            reactive = 0
            consumers = 0
            for lineno, key, blk in launched_effects(env):
                if "consumePendingEnvVarCreate(" not in blk:
                    continue
                consumers += 1
                if any(re.search(rf"\b{re.escape(v)}\b", key) for v in collected):
                    reactive += 1
                    continue
                fail(REL_ENV_SCREEN,
                     f"LaunchedEffect({key.strip()}) at line {lineno} still consumes the "
                     "pending create request — a one-shot effect never re-runs, so a "
                     "warm-start deep link into this screen is dropped"
                     + ("" if collected else
                        " (and no `by …pendingEnvVarCreate.collectAsState()` state exists "
                        "to key it on)"))
            if consumers == 0:
                fail(REL_ENV_SCREEN,
                     "no LaunchedEffect keyed on the observed pending-create state consumes "
                     "consumePendingEnvVarCreate() — the reactive binding is gone "
                     "(fail-closed: re-derive this criterion, never drop it)")
        # 10. the form's four shadow copies must be keyed on their source
        if "fun EnvVarFormSheet(" not in env:
            fail(REL_ENV_SCREEN,
                 "EnvVarFormSheet( not found (fail-closed: criterion 10 pins its field "
                 "seeding)")
        else:
            for field in ENV_FORM_FIELDS:
                decl = remember_decl(env, field)
                if decl is None:
                    fail(REL_ENV_SCREEN,
                         f"`var {field} by remember(...)` not found — the field is keyless "
                         "or renamed (fail-closed: it must be keyed on what it seeds from)")
                    continue
                keys, init = decl
                if not keys.strip():
                    fail(REL_ENV_SCREEN,
                         f"`var {field} by remember()` carries no keys — it keeps the first "
                         "entry's text after editEntry switches identity, and Save writes "
                         "that stale text back over an external write")
                    continue
                # [wide-1024 12-misc-b] "Non-empty key list" is not the contract:
                # `remember(Unit)` / `remember(true)` / `remember(0)` are just as
                # constant, and a list that drops ONE seed source keeps that half
                # stale. Derive what the initializer READS and require every root
                # of it to appear in the key list (editEntry?.key ?: prefillKey
                # needs both; editEntry?.group ?: "" needs only editEntry).
                seeds = ident_roots(init)
                missing = sorted(seeds - ident_roots(keys))
                if missing:
                    fail(REL_ENV_SCREEN,
                         f"`var {field} by remember({keys.strip()})` is not keyed on "
                         f"{', '.join(missing)} — the initializer seeds from it, so the "
                         "field keeps the previous entry's/prefill's value instead of "
                         "re-seeding when it changes")

    # --- 9. [da1028-C] ChatScreen observes its pending quick action ---------
    chat = code_only(read(root, REL_CHAT_SCREEN))
    if not chat:
        fail(REL_CHAT_SCREEN,
             "missing source file (fail-closed: this file hosts criterion 9)")
    else:
        if not re.search(r"pendingChatAction\s*\.\s*collectAsState\s*\(", chat):
            fail(REL_CHAT_SCREEN,
                 "pendingChatAction is not observed through collectAsState() — a quick "
                 "action delivered to an already-composed ChatScreen never opens the camera")
        if re.search(r"pendingChatAction\s*\.\s*value\b", chat):
            fail(REL_CHAT_SCREEN,
                 "pendingChatAction is still read via a `.value` snapshot — that read only "
                 "happens when the effect first composes")
        if not re.search(r"pendingHtmlPreview\w*\s*\.\s*collectAsState\s*\(", chat):
            fail(REL_CHAT_SCREEN,
                 "pendingHtmlPreview lost its collectAsState() subscription (regression "
                 "anchor: this subscription is the shape criterion 9 mirrors, it must not "
                 "be 'simplified' back to .value)")

    # --- 11. [da1029-A] cold-start dispatch names every DeepLinkAction case --
    # `minis://views/alarm` was a silent no-op on cold start: the sealed family
    # had 11 subclasses, the dispatch named 7 of them and swallowed the rest in
    # `else -> {}`, so a new subclass landed in a hole nobody could see.
    # (The neighbouring `quickActionStart` resolver keeps its `else -> null` —
    # it is selected out by requiring the dispatch-only OpenTerminal case.)
    dispatches = [
        b for b in when_blocks(nav, "when (initialDeepLink)")
        if "DeepLinkAction.OpenTerminal" in b
    ]
    if not dispatches:
        fail(REL_NAV,
             "cold-start `when (initialDeepLink)` dispatch block not found "
             "(renamed or restructured? re-anchor criterion 11, do not drop it)")
    else:
        dispatch = dispatches[0]
        for case in COLD_START_ACTION_CASES:
            if f"DeepLinkAction.{case}" not in dispatch:
                fail(REL_NAV,
                     f"cold-start dispatch has no `is DeepLinkAction.{case}` branch — "
                     "every sealed subclass must be named (an unnamed case falls into "
                     "the old catch-all hole, which is exactly how OpenAlarmList broke)")
        if re.search(r"\belse\s*->", dispatch):
            fail(REL_NAV,
                 "cold-start dispatch still ends in a catch-all `else ->` — name the "
                 "remaining subclasses explicitly so adding one is a compile error here "
                 "instead of a silent no-op")
        # The subject is `DeepLinkAction?`; with the catch-all gone the compiler
        # demands an explicit null branch ("'when' expression must be
        # exhaustive"). Mirror that at the source layer so the failure is caught
        # offline instead of on the CI compile pass.
        if re.search(r"initialDeepLink\s*:\s*DeepLinkAction\s*\?", nav) and \
                not re.search(r"(?:^|,)\s*null\s*->", dispatch, re.M):
            fail(REL_NAV,
                 "cold-start dispatch: `initialDeepLink` is nullable "
                 "(`DeepLinkAction?`) and the catch-all `else` is gone — the `when` "
                 "must name the `null` case explicitly (otherwise the compiler "
                 "rejects it: \"'when' expression must be exhaustive\")")
        # [wide-1024 12-misc-b] "The branch mentions ACTION_SHOW_ALARMS" only
        # proves the Intent was CONSTRUCTED. `showAlarms.action` (read the
        # property, launch nothing) kept the gate green while the card tap did
        # nothing — judge the side effect: the derived Intent must reach
        # startActivity() in the same branch.
        alarm = case_block(dispatch, r"is\s+DeepLinkAction\.OpenAlarmList\s*->")
        if alarm is None:
            fail(REL_NAV,
                 "cold-start dispatch: `is DeepLinkAction.OpenAlarmList` branch must launch "
                 "AlarmClock.ACTION_SHOW_ALARMS (minis://views/alarm was a cold-start "
                 "no-op; parity with MainActivity's warm-start handler)")
        elif "AlarmClock.ACTION_SHOW_ALARMS" not in alarm:
            fail(REL_NAV,
                 "cold-start dispatch: `is DeepLinkAction.OpenAlarmList` branch must launch "
                 "AlarmClock.ACTION_SHOW_ALARMS (minis://views/alarm was a cold-start "
                 "no-op; parity with MainActivity's warm-start handler)")
        else:
            built = re.search(r"val\s+(\w+)\s*=\s*[\w.]*Intent\s*\(", alarm)
            launched = False
            if built:
                launched = re.search(
                    rf"startActivity\s*\(\s*{re.escape(built.group(1))}\s*\)", alarm) is not None
            else:
                launched = re.search(
                    r"startActivity\s*\([^)]*Action|startActivity\s*\([^)]*ACTION_SHOW_ALARMS",
                    alarm) is not None
            if not launched:
                fail(REL_NAV,
                     "cold-start dispatch: the OpenAlarmList branch builds the "
                     "AlarmClock.ACTION_SHOW_ALARMS Intent but never hands it to "
                     "startActivity() — the tap is a silent no-op (fail-closed: derive the "
                     "Intent variable from `val x = …Intent(…)` and require startActivity(x))")


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_DLH = '''package com.rikkaminis.app.deeplink

internal fun resolveSettingsDeepLink(path: String, query: Map<String, String>): SettingsDeepLink {
    val segments = path.trimStart('/').split('/').filter { it.isNotEmpty() }
    val head = segments.firstOrNull() ?: return SettingsDeepLink.Route(Routes.SETTINGS)
    return when (head) {
        "providers" -> SettingsDeepLink.Route(Routes.providerDetail(head))
        "mcp-servers", "mcp_servers" -> SettingsDeepLink.Route(Routes.MCP)
        "permissions" -> SettingsDeepLink.PermissionSettings
        "environments" -> SettingsDeepLink.Route(Routes.ENV_VARS)
        "memory" -> SettingsDeepLink.Route(Routes.MEMORY)
        else -> SettingsDeepLink.Route(Routes.SETTINGS)
    }
}
'''

FIXTURE_NAV = '''package com.rikkaminis.app.ui.navigation

object Routes {
    internal fun seg(v: String): String = android.net.Uri.encode(v)
    fun providerDetail(instanceId: String) = "provider/${seg(instanceId)}"
    fun providerConnection(instanceId: String) = "provider_connection/${seg(instanceId)}"
    fun modelGroupDetail(groupId: String) = "model_group/${seg(groupId)}"
    fun addModelsToGroup(groupId: String) = "add_models_to_group/${seg(groupId)}"
    fun addCustomModel(instanceId: String) = "add_custom_model/${seg(instanceId)}"
    fun modelEntryDetail(instanceId: String, entryId: String) =
        "model_entry/${seg(instanceId)}/${seg(entryId)}"
    fun scheduledTaskEdit(taskId: String?) =
        if (taskId == null) "scheduled_task_edit/-" else "scheduled_task_edit/${seg(taskId)}"
    fun skillDetail(skillId: String) = "skill/${seg(skillId)}"
    fun skillFile(skillId: String, relativePath: String): String {
        val encoded = relativePath.replace("+", "%20")
        return "skill_file/${seg(skillId)}/$encoded"
    }
    fun chatFiles(sessionId: String) = "chat_files/${seg(sessionId)}"
    fun mountedFoldersDetail(mountId: String) = "mounted_folders_detail/${seg(mountId)}"
    fun sharedFoldersDetail(folderId: String) = "shared_folders_detail/${seg(folderId)}"
    fun logDetail(fileName: String) = "log_detail/${seg(fileName)}"
    fun sessionStorageDetail(sessionId: String) = "session_storage/${seg(sessionId)}"
    fun memoryFileEdit(fileName: String, isGlobal: Boolean) = "memory_file/${seg(fileName)}/$isGlobal"
    fun chat(sessionId: String, focusMessageId: String? = null): String {
        val base = "chat/${seg(sessionId)}"
        if (focusMessageId.isNullOrBlank()) return base
        return "$base?focusMessageId=$focusMessageId"
    }
    fun mirrorCategoryDetail(categoryId: String) = "mirror_category/${seg(categoryId)}"
}

suspend fun handleColdStartDeepLink(navController: NavController, initialDeepLink: DeepLinkAction?) {
    when (initialDeepLink) {
        is DeepLinkAction.OpenTerminal -> {
            navController.navigate(Routes.terminal(initialDeepLink.initCommand))
        }
        is DeepLinkAction.OpenSession -> {
            navController.navigate(Routes.chat(initialDeepLink.sessionId))
        }
        is DeepLinkAction.CreateEnvironmentVariable -> {
            navController.navigate(Routes.ENV_VARS)
        }
        is DeepLinkAction.OpenSettingsScreen -> {
            runCatching { navController.navigate(initialDeepLink.route) }
                .onFailure { runCatching { navController.navigate(Routes.SETTINGS) } }
        }
        is DeepLinkAction.OpenPermissionSettings -> {
            navController.navigate(Routes.PERMISSIONS)
        }
        is DeepLinkAction.OpenHtmlPreview -> {}
        is DeepLinkAction.NewChat,
        is DeepLinkAction.NewCameraChat -> {}
        is DeepLinkAction.OpenAlarmList -> {
            val showAlarms =
                android.content.Intent(android.provider.AlarmClock.ACTION_SHOW_ALARMS)
            context.startActivity(showAlarms)
        }
        is DeepLinkAction.OpenShare,
        is DeepLinkAction.Unknown,
        null -> {}
    }
}

fun NavGraphBuilder.modelEntryDetailGraph(navController: NavController) {
    composable(
        route = Routes.MODEL_ENTRY_DETAIL,
        arguments = listOf(
            navArgument("instanceId") { type = NavType.StringType },
            navArgument("entryId") { type = NavType.StringType },
        ),
    ) { backStackEntry ->
        val instanceId = backStackEntry.arguments?.getString("instanceId")
            ?: return@composable
        val entryId = backStackEntry.arguments?.getString("entryId")
            ?: return@composable
        ModelEntryDetailScreen(instanceId = instanceId, entryId = entryId)
    }
}
'''

FIXTURE_MAIN = '''package com.rikkaminis.app

private fun handleDeepLink() {
    runCatching { nav.navigate(action.route) }
        .onFailure { nav.navigate(Routes.SETTINGS) }
}
'''

# --- [da1028-C] fixtures: reactive deep-link consumers ----------------------

FIXTURE_ENV_SCREEN = '''package com.rikkaminis.app.ui.settings

@Composable
fun EnvironmentVariablesScreen() {
    val pendingCreate by DeepLinkCoordinator.pendingEnvVarCreate.collectAsState()
    LaunchedEffect(pendingCreate) {
        pendingCreate?.let {
            editEntryId = null
            prefill = it
            showAddSheet = true
            DeepLinkCoordinator.consumePendingEnvVarCreate()
        }
    }
}

@Composable
fun EnvVarFormSheet(editEntry: EnvVarEntry?, editEntryId: String?) {
    val currentSecretValue = editEntry?.let { envVarRepository.getValue(it.key) } ?: prefillValue
    var keyText by remember(editEntry?.id, editEntry?.key, prefillKey) {
        mutableStateOf(editEntry?.key ?: prefillKey)
    }
    var valueText by remember(editEntry?.id, currentSecretValue, prefillValue) {
        mutableStateOf(currentSecretValue)
    }
    var noteText by remember(editEntry?.id, editEntry?.note, prefillNote) {
        mutableStateOf(editEntry?.note ?: prefillNote)
    }
    var groupText by remember(editEntry?.id, editEntry?.group) {
        mutableStateOf(editEntry?.group ?: "")
    }
}
'''

# the pre-fix shape: one-shot consume, invisible to a warm start
FIXTURE_ENV_SINGLE_SHOT = '''package com.rikkaminis.app.ui.settings

@Composable
fun EnvironmentVariablesScreen() {
    LaunchedEffect(Unit) {
        DeepLinkCoordinator.consumePendingEnvVarCreate()?.let {
            prefill = it
            showAddSheet = true
        }
    }
}

@Composable
fun EnvVarFormSheet(editEntry: EnvVarEntry?, editEntryId: String?) {
    var keyText by remember(editEntry?.id, editEntry?.key, prefillKey) { mutableStateOf("") }
    var valueText by remember(editEntry?.id, currentSecretValue, prefillValue) { mutableStateOf("") }
    var noteText by remember(editEntry?.id, editEntry?.note, prefillNote) { mutableStateOf("") }
    var groupText by remember(editEntry?.id, editEntry?.group) { mutableStateOf("") }
}
'''

# the form still seeds each field once
FIXTURE_ENV_KEYLESS_FIELDS = FIXTURE_ENV_SCREEN.replace(
    "var keyText by remember(editEntry?.id, editEntry?.key, prefillKey) {",
    "var keyText by remember {",
).replace(
    "var groupText by remember(editEntry?.id, editEntry?.group) {",
    "var groupText by remember {",
)

# `remember()` with an empty key list is the same defect wearing a hat
FIXTURE_ENV_EMPTY_KEY_LIST = FIXTURE_ENV_SINGLE_SHOT \
    .replace("var keyText by remember(editEntry?.id, editEntry?.key, prefillKey)",
             "var keyText by remember()") \
    .replace("var valueText by remember(editEntry?.id, currentSecretValue, prefillValue)",
             "var valueText by remember()") \
    .replace("var noteText by remember(editEntry?.id, editEntry?.note, prefillNote)",
             "var noteText by remember()") \
    .replace("var groupText by remember(editEntry?.id, editEntry?.group)",
             "var groupText by remember()")

FIXTURE_CHAT_SCREEN = '''package com.rikkaminis.app.ui.chat
@Composable
fun ChatScreen(sessionId: String) {
    val pendingChatActionState by com.rikkaminis.app.deeplink.DeepLinkCoordinator
        .pendingChatAction.collectAsState()
    LaunchedEffect(sessionId, pendingChatActionState) {
        if (pendingChatActionState == com.rikkaminis.app.deeplink.DeepLinkCoordinator
                .ChatAction.OPEN_CAMERA
        ) {
            com.rikkaminis.app.deeplink.DeepLinkCoordinator.consumePendingChatAction()
            launchCamera()
        }
    }
    val pendingHtmlPreviewState by com.rikkaminis.app.deeplink.DeepLinkCoordinator
        .pendingHtmlPreview.collectAsState()
    LaunchedEffect(sessionId, pendingHtmlPreviewState) {
        pendingHtmlPreviewState?.let { openHtmlPreview(it) }
    }
}
'''

# the pre-fix shape: `.value` snapshot
FIXTURE_CHAT_STATIC_READ = '''package com.rikkaminis.app.ui.chat

@Composable
fun ChatScreen(sessionId: String) {
    LaunchedEffect(sessionId) {
        val pending = com.rikkaminis.app.deeplink.DeepLinkCoordinator
            .pendingChatAction.value
        if (pending == com.rikkaminis.app.deeplink.DeepLinkCoordinator
                .ChatAction.OPEN_CAMERA
        ) {
            com.rikkaminis.app.deeplink.DeepLinkCoordinator.consumePendingChatAction()
            launchCamera()
        }
    }
    val pendingHtmlPreviewState by com.rikkaminis.app.deeplink.DeepLinkCoordinator
        .pendingHtmlPreview.collectAsState()
    LaunchedEffect(sessionId, pendingHtmlPreviewState) {
        pendingHtmlPreviewState?.let { openHtmlPreview(it) }
    }
}
'''

# regression anchor: the neighbouring subscription was "simplified" away
FIXTURE_CHAT_HTML_PREVIEW_REGRESSED = FIXTURE_CHAT_SCREEN.replace(
    "    val pendingHtmlPreviewState by com.rikkaminis.app.deeplink.DeepLinkCoordinator\n"
    "        .pendingHtmlPreview.collectAsState()\n"
    "    LaunchedEffect(sessionId, pendingHtmlPreviewState) {\n"
    "        pendingHtmlPreviewState?.let { openHtmlPreview(it) }\n"
    "    }\n",
    "    LaunchedEffect(sessionId) {\n"
    "        com.rikkaminis.app.deeplink.DeepLinkCoordinator.pendingHtmlPreview.value\n"
    "            ?.let { openHtmlPreview(it) }\n"
    "    }\n",
)


def write_fixture(root: Path, dlh: str, nav: str, main: str,
                  env: str = FIXTURE_ENV_SCREEN, chat: str = FIXTURE_CHAT_SCREEN) -> None:
    for rel, content in ((REL_DLH, dlh), (REL_NAV, nav), (REL_MAIN, main),
                         (REL_ENV_SCREEN, env), (REL_CHAT_SCREEN, chat)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def selftest() -> None:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # compliant fixture must pass
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if VIOLATIONS:
            ok = False
            print(f"SELFTEST compliant fixture unexpectedly failed: {VIOLATIONS}")

        # fixture B: drop the mcp-servers whitelist entry -> must fail
        write_fixture(root, FIXTURE_DLH.replace('"mcp-servers", "mcp_servers" ->', '->'), FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("mcp-servers" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture B (drop mcp-servers) did not trip the gate")
        else:
            print("SELFTEST B (drop whitelist entry) RED as expected")

        # fixture C: remove the seg() encoding from providerDetail -> must fail
        bad_nav = FIXTURE_NAV.replace('"provider/${seg(instanceId)}"', '"provider/$instanceId"')
        write_fixture(root, FIXTURE_DLH, bad_nav, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("providerDetail" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture C (raw providerDetail) did not trip the gate")
        else:
            print("SELFTEST C (drop encode) RED as expected")

        # fixture D: fallback turned into an error -> must fail
        bad_dlh = FIXTURE_DLH.replace("else -> SettingsDeepLink.Route(Routes.SETTINGS)", "else -> SettingsDeepLink.Unknown")
        write_fixture(root, bad_dlh, FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("fallback" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture D (fallback removed) did not trip the gate")
        else:
            print("SELFTEST D (fallback removed) RED as expected")

        # fixture E: navigate backstop removed -> must fail
        bad_main = FIXTURE_MAIN.replace("runCatching { nav.navigate(action.route) }", "nav.navigate(action.route)")
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, bad_main)
        VIOLATIONS.clear()
        check(root)
        if not any("runCatching" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture E (backstop removed) did not trip the gate")
        else:
            print("SELFTEST E (backstop removed) RED as expected")

        # fixture F: [da1021-C] cold-start backstop removed -> must fail
        bad_nav_cold = FIXTURE_NAV.replace(
            "runCatching { navController.navigate(initialDeepLink.route) }",
            "navController.navigate(initialDeepLink.route)",
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_cold, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("cold-start" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture F (cold-start backstop removed) did not trip the gate")
        else:
            print("SELFTEST F (cold-start backstop removed) RED as expected")

        # fixture G: [da1021-C] raw mirrorCategoryDetail -> must fail
        bad_nav_mirror = FIXTURE_NAV.replace(
            '"mirror_category/${seg(categoryId)}"', '"mirror_category/$categoryId"'
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_mirror, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("mirrorCategoryDetail" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture G (raw mirrorCategoryDetail) did not trip the gate")
        else:
            print("SELFTEST G (raw mirrorCategoryDetail) RED as expected")

        # fixture H: [da1024-E] ONE second decode back inside the composable
        # block -> exactly one decode violation, and the fail-closed guard
        # survives (so criterion 7 must stay green: each criterion has a
        # fixture that breaks it and one that does not).
        bad_nav_h = FIXTURE_NAV.replace(
            'val instanceId = backStackEntry.arguments?.getString("instanceId")\n'
            '            ?: return@composable',
            'val instanceId = backStackEntry.arguments?.getString("instanceId")\n'
            '            ?.let { android.net.Uri.decode(it) } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_h, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        guard = [v for v in VIOLATIONS if "fail-closed guard" in v]
        if len(dec) != 1 or guard:
            ok = False
            print(f"SELFTEST fixture H (one second decode) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST H (one second decode) RED as expected")

        # fixture I: both args decoded again -> two decode violations (the
        # criterion counts sites, it is not satisfied by the first hit).
        bad_nav_i = bad_nav_h.replace(
            'val entryId = backStackEntry.arguments?.getString("entryId")\n'
            '            ?: return@composable',
            'val entryId = backStackEntry.arguments?.getString("entryId")\n'
            '            ?.let { android.net.Uri.decode(it) } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_i, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        if len(dec) != 2:
            ok = False
            print(f"SELFTEST fixture I (both decoded again) expected 2 violations, got {len(dec)}")
        else:
            print("SELFTEST I (both decoded again) RED as expected")

        # fixture I2: URLDecoder variant of the same defect -> still RED.
        bad_nav_i2 = bad_nav_h.replace(
            "?.let { android.net.Uri.decode(it) } ?: return@composable",
            '?.let { java.net.URLDecoder.decode(it, "UTF-8") } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_i2, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not [v for v in VIOLATIONS if "decodes a route argument" in v]:
            ok = False
            print("SELFTEST fixture I2 (URLDecoder double decode) did not trip the gate")
        else:
            print("SELFTEST I2 (URLDecoder double decode) RED as expected")

        # fixture J: fail-closed guard dropped (no decode at all) -> only
        # criterion 7 may fire.
        bad_nav_j = FIXTURE_NAV.replace(
            'arguments?.getString("instanceId")\n            ?: return@composable',
            'arguments?.getString("instanceId") ?: ""',
        ).replace(
            'arguments?.getString("entryId")\n            ?: return@composable',
            'arguments?.getString("entryId") ?: ""',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_j, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        guard = [v for v in VIOLATIONS if "fail-closed guard" in v]
        if not guard or dec:
            ok = False
            print(f"SELFTEST fixture J (guard dropped) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST J (fail-closed guard dropped) RED as expected")

        # --- [da1028-C] fixtures K..O: reactive deep-link consumers ---------
        # fixture K: the one-shot consume shape -> two violations (no
        # collectAsState + a LaunchedEffect(Unit) that consumes).
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN,
                      env=FIXTURE_ENV_SINGLE_SHOT)
        VIOLATIONS.clear()
        check(root)
        sub = [v for v in VIOLATIONS if "pendingEnvVarCreate is not observed" in v]
        one = [v for v in VIOLATIONS if "still consumes the pending create request" in v]
        if not sub or not one:
            ok = False
            print(f"SELFTEST K (one-shot consume) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST K (one-shot deep-link consume) RED as expected")

        # fixture L: the form seeds each field once (the pre-fix keyless shape)
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN,
                      env=FIXTURE_ENV_KEYLESS_FIELDS)
        VIOLATIONS.clear()
        check(root)
        keyless = [v for v in VIOLATIONS if "the field is keyless or renamed" in v]
        if len(keyless) != 2 or not all("keyText" in v or "groupText" in v for v in keyless):
            ok = False
            print(f"SELFTEST L (keyless form fields) expected 2 violations, got {keyless}")
        else:
            print("SELFTEST L (keyless form fields) RED as expected")

        # fixture L2: `remember()` with an empty key list is the same defect
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN,
                      env=FIXTURE_ENV_EMPTY_KEY_LIST)
        VIOLATIONS.clear()
        check(root)
        empty_keys = [v for v in VIOLATIONS if "carries no keys" in v]
        if len(empty_keys) != 4:
            ok = False
            print(f"SELFTEST L2 (empty key list) expected 4 violations, got {empty_keys}")
        else:
            print("SELFTEST L2 (empty key list) RED as expected")

        # fixture M: ChatScreen keeps the `.value` snapshot
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN,
                      chat=FIXTURE_CHAT_STATIC_READ)
        VIOLATIONS.clear()
        check(root)
        static = [v for v in VIOLATIONS if "still read via a `.value` snapshot" in v]
        missing = [v for v in VIOLATIONS if "pendingChatAction is not observed" in v]
        if not static or not missing:
            ok = False
            print(f"SELFTEST M (static quick-action read) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST M (static quick-action read) RED as expected")

        # fixture N: the neighbouring pendingHtmlPreview subscription regressed
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN,
                      chat=FIXTURE_CHAT_HTML_PREVIEW_REGRESSED)
        VIOLATIONS.clear()
        check(root)
        if not [v for v in VIOLATIONS if "pendingHtmlPreview lost its collectAsState()" in v]:
            ok = False
            print(f"SELFTEST N (pendingHtmlPreview regression anchor) missed: {VIOLATIONS}")
        else:
            print("SELFTEST N (pendingHtmlPreview regression anchor) RED as expected")

        # fixture O: precision — editing an unrelated file (the deep-link
        # resolver) must not make criteria 8/9/10 speak up.
        write_fixture(root, FIXTURE_DLH.replace(
            '"memory" -> SettingsDeepLink.Route(Routes.MEMORY)',
            '"memory", "memories" -> SettingsDeepLink.Route(Routes.MEMORY)',
        ), FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        noise = [v for v in VIOLATIONS if REL_ENV_SCREEN + ":" in v or REL_CHAT_SCREEN + ":" in v]
        if noise:
            ok = False
            print(f"SELFTEST O (unrelated edit) leaked into the consumer criteria: {noise}")
        else:
            print("SELFTEST O (unrelated edit stays silent) GREEN as expected")

        # --- [da1029-A] criterion 11 ---------------------------------------
        # fixture P: drop the OpenAlarmList branch -> must fail
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "        is DeepLinkAction.OpenAlarmList -> {\n"
            "            val showAlarms =\n"
            "                android.content.Intent(android.provider.AlarmClock.ACTION_SHOW_ALARMS)\n"
            "            context.startActivity(showAlarms)\n"
            "        }\n",
            "",
        ), FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("OpenAlarmList" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST P (drop OpenAlarmList branch) did not trip criterion 11")
        else:
            print("SELFTEST P (drop OpenAlarmList branch) RED as expected")

        # fixture Q: put the catch-all `else -> {}` back -> must fail
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "        is DeepLinkAction.OpenShare,\n"
            "        is DeepLinkAction.Unknown,\n        null -> {}",
            "        else -> {}",
        ), FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("catch-all" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST Q (catch-all else restored) did not trip criterion 11")
        else:
            print("SELFTEST Q (catch-all else restored) RED as expected")

        # fixture R: OpenAlarmList branch launches the WRONG Clock action ->
        # the branch presence alone must not satisfy the criterion.
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "android.provider.AlarmClock.ACTION_SHOW_ALARMS",
            "android.provider.AlarmClock.ACTION_SET_ALARM",
        ), FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("ACTION_SHOW_ALARMS" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST R (wrong alarm action) did not trip criterion 11")
        else:
            print("SELFTEST R (wrong alarm action) RED as expected")

        # fixture S: precision — a SECOND `when (initialDeepLink)` resolver
        # carrying `else -> null` (the real `quickActionStart` shape) must not
        # make criterion 11 speak up: it selects the dispatch block by its
        # dispatch-only OpenTerminal case.
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "fun NavGraphBuilder.modelEntryDetailGraph",
            "fun quickActionStartOf(initialDeepLink: DeepLinkAction): String? =\n"
            "    when (initialDeepLink) {\n"
            '        is DeepLinkAction.NewChat -> Routes.chat("x")\n'
            "        else -> null\n"
            "    }\n\n"
            "fun NavGraphBuilder.modelEntryDetailGraph",
        ), FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        noise = [v for v in VIOLATIONS if "dispatch" in v]
        if noise:
            ok = False
            print(f"SELFTEST S (resolver else -> null) leaked into criterion 11: {noise}")
        else:
            print("SELFTEST S (startDestination resolver stays out of scope) GREEN as expected")

        # fixture T [da1029-A]: nullable subject with the `null ->` case dropped.
        # The catch-all is gone, so this is a hard compile error in the real
        # build ("'when' expression must be exhaustive") — the gate mirrors it
        # offline instead of spending a CI round on it.
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "        is DeepLinkAction.Unknown,\n        null -> {}\n", "",
        ), FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("nullable" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST T (nullable subject, null case dropped) did not trip criterion 11")
        else:
            print("SELFTEST T (nullable subject, null case dropped) RED as expected")

        # fixture U [da1029-A] precision: a NON-nullable subject needs no `null`
        # branch, so dropping it must stay silent.
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV.replace(
            "initialDeepLink: DeepLinkAction?", "initialDeepLink: DeepLinkAction",
        ).replace("        is DeepLinkAction.Unknown,\n        null -> {}\n", ""),
            FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if any("nullable" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST U (non-nullable subject) leaked the nullable criterion")
        else:
            print("SELFTEST U (non-nullable subject needs no null branch) GREEN as expected")

        # --- [wide-1024 12-misc-b] witness arms: the four shapes the report
        # proved blind. Each one keeps every substring the old criteria looked
        # for and drops the EFFECT, so each arm asserts the new criterion speaks
        # AND nothing else does.
        def witness_arm(label, needle, nav=FIXTURE_NAV, env=FIXTURE_ENV_SCREEN):
            nonlocal ok
            write_fixture(root, FIXTURE_DLH, nav, FIXTURE_MAIN, env=env)
            VIOLATIONS.clear()
            check(root)
            hits = [v for v in VIOLATIONS if needle in v]
            noise = [v for v in VIOLATIONS if needle not in v]
            if not hits or noise or len(hits) != 1:
                ok = False
                print(f"SELFTEST {label} wrong verdict: {VIOLATIONS}")
            else:
                print(f"SELFTEST {label} RED as expected")

        # V: one encoded sibling + one raw composite id (entryId carries '/').
        witness_arm(
            "V (raw sibling id in a 2-arg route builder)",
            "Routes.modelEntryDetail interpolates raw id",
            nav=FIXTURE_NAV.replace(
                '"model_entry/${seg(instanceId)}/${seg(entryId)}"',
                '"model_entry/${seg(instanceId)}/$entryId"',
            ),
        )
        # W: constant LaunchedEffect key — same defect as Unit, different spelling.
        witness_arm(
            "W (LaunchedEffect(true) keeps the consume)",
            "still consumes the pending create request",
            env=FIXTURE_ENV_SCREEN.replace(
                "LaunchedEffect(pendingCreate) {", "LaunchedEffect(true) {"),
        )
        # X: constant remember key.
        witness_arm(
            "X (remember(Unit) field shadow)",
            "`var keyText by remember(Unit)` is not keyed on",
            env=FIXTURE_ENV_SCREEN.replace(
                "var keyText by remember(editEntry?.id, editEntry?.key, prefillKey) {",
                "var keyText by remember(Unit) {"),
        )
        # X2: half a key list — the prefill half goes stale.
        witness_arm(
            "X2 (key list drops the prefill half)",
            "is not keyed on prefillKey",
            env=FIXTURE_ENV_SCREEN.replace(
                "var keyText by remember(editEntry?.id, editEntry?.key, prefillKey) {",
                "var keyText by remember(editEntry?.id, editEntry?.key) {"),
        )
        # Y: the Intent is built and then only read — never launched.
        witness_arm(
            "Y (alarm Intent built, never launched)",
            "never hands it to startActivity()",
            nav=FIXTURE_NAV.replace(
                "context.startActivity(showAlarms)", "showAlarms.action"),
        )

    if not ok:
        sys.exit("DEEPLINK_SETTINGS_ROUTES_SELFTEST=FAIL")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        print("DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS")
        return
    root = Path(args[0]) if args else Path.cwd()
    if not root.exists():
        sys.exit(f"FAIL: root {root} does not exist")
    check(root)
    if VIOLATIONS:
        print(f"FAIL: {len(VIOLATIONS)} violation(s)")
        for v in VIOLATIONS:
            print(f"  {v}")
        sys.exit(1)
    print("DEEPLINK_SETTINGS_ROUTES_CHECK=PASS")


if __name__ == "__main__":
    main()
