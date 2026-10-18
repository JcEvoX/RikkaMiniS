#!/usr/bin/env python3
"""Guard: every SharedPreferences change listener must be strongly held.

Why this exists (F-134): AOSP `SharedPreferencesImpl` stores listeners in

    private final WeakHashMap<OnSharedPreferenceChangeListener, Object> mListeners;

whose *value* is a static sentinel (`CONTENT`) that does NOT reference the
key. A listener created as a bare lambda **argument** therefore has no strong
reference anywhere: the registering frame returns, the GC collects the
listener, and the callback silently stops firing. It is never re-registered.

Nothing about that failure is observable — the write lands in prefs, the
registration call already returned success, and no exception is thrown. The
only symptom is "I set the knob and it kept using the old value", which is
indistinguishable from the value never having been read. That is a
silent-failure shape, so it gets a mechanical guard.

This is not hypothetical: ConfigBuiltins registered a bare lambda and
`minis-config set runtime.shellOutputKb 512` returned ok:true while every
reader kept serving the primed default (128 KB) until process restart.

Shape accepted: the listener is bound to a name (`val listener = ...`, or a
property) that is held by something process- or screen-scoped, and THAT name
is what gets registered. The guard checks the argument is an identifier, and
that the identifier is either a field/property of an enclosing class/object
(process- or instance-scoped) or a local that is also stored into a field.

Escape hatch: `prefs-listener-ok: <reason>` on the same or the previous line.

Second concern ([fix/da1026-prefs-priming]) -- the same registration also has to
*re-prime* the cache it feeds. `ConcurrencyPrefs` and `AgentRuntimeLimitsPrefs`
keep their values in @Volatile fields primed once at startup, while their
minis-config fields write straight to SharedPreferences: a write from outside
the Settings screens (minis-config set / backup restore / the agent) changed the
stored value while every context-free reader kept the startup value -- the same
"changed but takes effect never" shape, one layer down. Criteria C1/C2/C3 (see
their own section below) require the re-priming listener to exist in the file
that registers the config path, and to prime before it reads.

`--selftest` runs six miniature-tree fixtures, each broken in exactly one way,
and prints `PREFS_PRIMING_SELFTEST=PASS` when all six behave.
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

REGISTER_RE = re.compile(r"\.registerOnSharedPreferenceChangeListener\b")
ESCAPE_RE = re.compile(r"prefs-listener-ok\s*:")

# A lambda / anonymous listener passed inline as the argument. Must cover BOTH
# call shapes, because the hazardous one is the trailing-lambda form:
#     prefs.registerOnSharedPreferenceChangeListener { _, _ -> ... }   (no parens)
#     prefs.registerOnSharedPreferenceChangeListener(Listener { _, _ -> })
# An earlier version of this guard required `\(` and therefore silently skipped
# the trailing-lambda form -- i.e. it reported OK on the exact line it exists to
# catch. Verified by running it against the pre-fix tree (must FAIL there).
INLINE_RE = re.compile(
    r"\.registerOnSharedPreferenceChangeListener\s*(?:\(\s*)?"
    r"(?:\{|[A-Za-z_][\w.]*\s*\{)"
)

problems = []
sites = []


def _strip_comments_and_strings(text: str) -> str:
    """Blank out // comments, /* */ blocks and string literals (brace-safe)."""
    out = []
    i, n = 0, len(text)
    state = None  # None | "line" | "block" | "str" | "raw"
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
            if c == "\n":
                state = None; out.append("\n")
            else:
                out.append(" ")
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
            out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def field_declarations(text: str) -> set:
    """Names declared as class/object-level properties (NOT locals inside a fun).

    Needs a scope stack: a `val` inside a function body is a local, and a local
    that is never stored elsewhere is exactly as collectable as an inline lambda
    -- that is the second hazard this guard must catch. An earlier version
    treated every `val`/`var` as a field, so `val l = ...; register(l)` passed.

    Two separate buffers, because the two jobs need different lifetimes:

      * `pending` resets at every line break and feeds the declaration regex --
        a `val`/`var` declaration never spans lines in this codebase.
      * `head` resets only at a top-level `{`/`}`/`;` and classifies the scope a
        `{` opens. It MUST span lines, and it must receive a separator at each
        line break.

    Both of those `head` requirements are load-bearing, and both were violated:

      1. `head` used to reset at every newline. A multi-line parameter list puts
         the `{` on its own line, so it saw only `) ` and classified the body as
         a class.
      2. Even with (1) fixed, `head` was concatenated without a separator, so an
         annotation on its own line fused with the keyword: `@Composable\nfun`
         became `@Composablefun`, `\bfun\b` could not match, and the body was
         classified as a class again.

    Either way, every local `val listener` inside such a function was recorded as
    a *field*, so the guard reported `held` for four real sites (ChatScreen,
    AppearanceScreen, ChatMenuSettingsScreen, MemoryManagementScreen) whose only
    holder is a sibling `onDispose` -- and deleting that `onDispose` still
    passed. `paren` is tracked for the same reason: a brace nested in parentheses
    is a lambda argument (e.g. a default `= {}`), not a new statement head.
    Regression fixtures live in `test_scan.py`.
    """
    clean = _strip_comments_and_strings(text)
    fields = set()
    stack = []          # each entry: "fun" | "class" | "other"
    pending = ""        # since the last line break -- for declaration detection
    head = ""           # since the last TOP-LEVEL brace/semicolon -- scope classification
    paren = 0           # () nesting depth, so a default lambda `= {}` can't reset head
    decl_re = re.compile(r"(?:^|[;{}])\s*(?:@\w+\s+)*(?:private\s+|internal\s+|public\s+|"
                         r"protected\s+|open\s+|override\s+|lateinit\s+|const\s+|@\w+\s+)*"
                         r"(val|var)\s+(\w+)")

    for ch in clean:
        if ch == "\n":
            pending = ""
            # `head` spans lines, so it needs a separator here. Without one the
            # annotation and the keyword fuse into a single token --
            # `@Composable\nfun` became `@Composablefun`, `\bfun\b` could not
            # match, and the function body was classified as a class. That is
            # what mislabelled the four real `DisposableEffect` sites.
            head += " "
            continue
        if ch == "(" or ch == ")":
            paren += 1 if ch == "(" else -1
            if paren < 0:
                paren = 0
            pending += ch
            head += ch
            continue
        if ch == "{":
            if re.search(r"\bfun\b", head):
                stack.append("fun")
            elif re.search(r"\b(class|object|interface)\b", head):
                stack.append("class")
            else:
                stack.append("other")
            pending = ""
            # A brace nested inside parentheses is a lambda argument (e.g. a
            # parameter default `= {}`), NOT a new statement head -- clearing
            # `head` there would erase the `fun` of a multi-line signature and
            # misclassify the whole function body as a class.
            if paren == 0:
                head = ""
            continue
        if ch == "}":
            if stack:
                stack.pop()
            pending = ""
            if paren == 0:
                head = ""
            continue
        if ch == ";":
            pending = ""
            if paren == 0:
                head = ""
            continue
        pending += ch
        head += ch
        # A declaration only counts as a field when no function body encloses it.
        m = decl_re.search(pending)
        if m and "fun" not in stack:
            fields.add(m.group(2))
    return fields


def ref_count(text: str, ident: str) -> int:
    """Occurrences of the identifier as a standalone token."""
    return len(re.findall(rf"\b{re.escape(ident)}\b", _strip_comments_and_strings(text)))


def stored_into_something(text: str, ident: str) -> bool:
    """`x = ident` / `val y = ident` -- the local escapes into another holder."""
    clean = _strip_comments_and_strings(text)
    return bool(re.search(rf"=\s*{re.escape(ident)}\s*(?:\n|$|\))", clean))


def inside_on_dispose(text: str, ident: str) -> bool:
    """True when `ident` is captured by an `onDispose { ... }` block.

    The Compose shape is a legitimate strong holder:

        DisposableEffect(prefs) {
            val listener = OnSharedPreferenceChangeListener { _, _ -> ... }
            prefs.registerOnSharedPreferenceChangeListener(listener)
            onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }
        }

    `onDispose`'s lambda captures `listener`, and the effect framework holds
    that lambda until disposal -- so the listener stays reachable for exactly
    as long as it is registered.
    """
    clean = _strip_comments_and_strings(text)
    for m in re.finditer(r"\bonDispose\s*\{", clean):
        depth, i, n = 1, m.end(), len(clean)
        while i < n and depth:
            if clean[i] == "{":
                depth += 1
            elif clean[i] == "}":
                depth -= 1
            i += 1
        body = clean[m.end():i]
        if re.search(rf"\b{re.escape(ident)}\b", body):
            return True
    return False


# ==========================================================================
# [fix/da1026-prefs-priming] Re-priming criteria.
#
# ConcurrencyPrefs and AgentRuntimeLimitsPrefs each cache their values in
# @Volatile fields that MinisApp.onCreate primes exactly once. Their
# minis-config fields (PrefsIntField) write STRAIGHT to SharedPreferences, so
# a write that does not come from the Settings screens (minis-config set /
# backup restore / the agent) lands in the file while every context-free
# reader keeps serving the value taken at startup -- the same "changed but
# takes effect never" shape the holder check above exists to prevent.
# AgentRuntimeLimitsPrefs had a re-priming listener; ConcurrencyPrefs had
# NONE, and RuntimeLimitsScreen's own listener read the stale cache and then
# re-persisted it on Save (silently rolling the external write back).
#
# Criteria. Every one is fail-closed: once a file is in scope, a missing
# anchor is RED, never a silent skip. The SCOPE is content-based rather than
# path-based, so the criteria hold wherever the code moves to.
#   C1  a file that registers the config path "runtime.maxConcurrentSessions"
#       must bind a handle to ConcurrencyPrefs.PREFS, register a listener on
#       that handle, and that listener must call ConcurrencyPrefs.prime(...)
#   C2  the same file must do the AgentRuntimeLimitsPrefs half (its knobs are
#       registered there too, so both re-primers must live side by side)
#   C3  in every change-listener body that reads
#       ConcurrencyPrefs.maxConcurrentSessions(), ConcurrencyPrefs.prime(...)
#       must appear BEFORE the read -- an unprimed read is what makes this
#       screen display, then re-persist, a value from before the write that
#       triggered the callback
# ==========================================================================

_PRIMING_CFG_PATH = '"runtime.maxConcurrentSessions"'
_PRIMING_PAIRS = (
    ("ConcurrencyPrefs", "ConcurrencyPrefs.prime("),
    ("AgentRuntimeLimitsPrefs", "AgentRuntimeLimitsPrefs.prime("),
)
_PRIMING_CAP_READ = "ConcurrencyPrefs.maxConcurrentSessions()"


def _brace_body(text, open_idx):
    """Text between the '{' at open_idx and its match (None when unbalanced)."""
    depth, i, n = 1, open_idx + 1, len(text)
    while i < n and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    return text[open_idx + 1:i - 1]


def _handle_bindings(clean, anchor):
    """`val H = ...getSharedPreferences(<anchor>...)` -> {H: start offset}."""
    pat = re.compile(
        r"\bval\s+(\w+)\s*=\s*[^\n]*?getSharedPreferences\(\s*(?:[\w.]+\.)?"
        + re.escape(anchor) + r"\b"
    )
    return {m.group(1): m.start() for m in pat.finditer(clean)}


def _listener_bodies(clean):
    """[(declared name|None, body)] for each OnSharedPreferenceChangeListener lambda.

    The declaration sits *before* the type name -- `val NAME = SharedPreferences.`
    -- so the head window must end mid-expression, not at a complete pattern.
    """
    out = []
    for m in re.finditer(r"OnSharedPreferenceChangeListener\s*\{", clean):
        head = clean[max(0, m.start() - 200):m.start()]
        dm = re.search(r"\bval\s+(\w+)\s*=\s*([^\n=]*)$", head)
        name = None
        if dm and re.fullmatch(r"(?:[\w.]+\.)?", dm.group(2).strip()):
            name = dm.group(1)
        out.append((name, _brace_body(clean, m.end() - 1) or ""))
    return out


def _kt_files(root):
    for path in sorted(root.rglob("*.kt")):
        sp = str(path)
        if "/build/" in sp or "/test/" in sp:
            continue
        yield path


def scan_priming(root):
    """Re-priming criteria. Returns (report lines, problems)."""
    report = []
    problems = []
    files = []
    for path in _kt_files(root):
        raw = path.read_text(encoding="utf-8", errors="replace")
        files.append((path, raw, _strip_comments_and_strings(raw)))

    anchored = [f for f in files if _PRIMING_CFG_PATH in f[1]]
    if not anchored and any("ConcurrencyPrefs" in f[2] for f in files):
        problems.append(
            'no file registers the config path %s although the tree still references '
            "ConcurrencyPrefs -- the anchor was renamed/removed, re-point this criterion"
            % _PRIMING_CFG_PATH
        )

    for path, _raw, clean in anchored:
        rel = path.relative_to(root)
        for obj, prime_call in _PRIMING_PAIRS:
            handles = _handle_bindings(clean, obj + ".PREFS")
            if not handles:
                problems.append(
                    "%s registers %s but binds no handle to %s.PREFS -- the re-priming "
                    "listener cannot exist" % (rel, _PRIMING_CFG_PATH, obj)
                )
                continue
            idents = [
                m.group(2).split(".")[-1]
                for m in re.finditer(
                    r"(\w+)\.registerOnSharedPreferenceChangeListener\s*\(\s*([\w.]+)", clean
                )
                if m.group(1) in handles
            ]
            if not idents:
                problems.append(
                    "%s binds %s.PREFS but has no resolvable change-listener registration on it "
                    "(expected `<handle>.registerOnSharedPreferenceChangeListener(<name>)`) -- "
                    "writes to that file would never re-prime the cache"
                    % (rel, obj)
                )
                continue
            bodies = _listener_bodies(clean)
            for ident in idents:
                found = [b for n, b in bodies if n == ident]
                if not found:
                    problems.append(
                        "%s registers `%s` on %s.PREFS but that listener's body could not be "
                        "located -- cannot prove it re-primes" % (rel, ident, obj)
                    )
                elif not any(prime_call in b for b in found):
                    problems.append(
                        "%s re-primes nothing: the listener `%s` on %s.PREFS never calls "
                        "%s...) -- a write to that file changes the stored value while every "
                        "cached reader keeps the value taken at startup"
                        % (rel, ident, obj, prime_call)
                    )
                else:
                    report.append("  %s: %s re-primed by `%s`" % (rel, obj, ident))

    cap_hits = 0
    for path, _raw, clean in files:
        if "ConcurrencyPrefs.PREFS" not in clean:
            continue
        rel = path.relative_to(root)
        for name, body in _listener_bodies(clean):
            at = body.find(_PRIMING_CAP_READ)
            if at < 0:
                continue
            cap_hits += 1
            prime_at = body.find("ConcurrencyPrefs.prime(")
            if prime_at < 0 or prime_at > at:
                problems.append(
                    "%s: the listener `%s` reads %s before priming the cache -- the read can "
                    "serve the value from before the write that triggered the callback, and "
                    "this screen re-persists every knob it shows on Save"
                    % (rel, name or "<inline>", _PRIMING_CAP_READ)
                )
            else:
                report.append("  %s: listener primes before reading the cap" % rel)

    if anchored and cap_hits == 0:
        problems.append(
            "no change-listener body reads %s any more -- the ordering criterion has nothing "
            "to check (did the screen stop showing the cap?)" % _PRIMING_CAP_READ
        )
    return report, problems


# ==========================================================================
# [ARCH-02] "PrimedPrefs" self-sync contract.
#
# Concern: the two criteria families above both require an *external* module to
# remember something -- F-134's holder discipline for whoever writes the
# registration by hand, and C1/C2 for whoever owns the config path. Neither can
# help a cache whose file is written by a path that has no listener at all.
# ARCH-02 moves the refresh *into* the object that owns the cache: `prime(...)`
# registers an object-owned, strongly-held listener that reloads the snapshot.
# Then the only thing anyone must remember is calling `prime`, which
# MinisApp.onCreate already does for all three.
#
#   C4  the file that *declares the cache's prefs file name*
#       (`const val PREFS = "minis_…_prefs"`) must contain an object that
#       satisfies S1-S3 for it -- the literal is the cache's identity anchor
#   C5  shape anchor (name-independent, so a 4th primed cache is covered the
#       day it appears): any `object` exposing `fun prime(<x>: Context)` over a
#       `@Volatile private var cached…` snapshot must satisfy S1-S3
#   C6  both drift directions of the two anchors: an object that still exists
#       while its literal is gone (C4/C5 go blind), and a literal with no
#       object left to declare it
#   C7  the in-scope object must not retain a `Context`-typed field -- the
#       listener has to take the `SharedPreferences` the callback hands it
#       (F-235's dead `appContext` field, re-opened by this very refactor)
#
#   S1  an object-owned listener handle is declared
#   S2  that handle is registered from inside `prime(...)`
#   S3  its body actually refreshes the snapshot (`cached… = …`, or a call to
#       the shared loader) -- an empty or `TODO()` listener satisfies S1+S2
#       while refreshing nothing, which is the silent-failure shape again
# ==========================================================================

_ARCH02_CACHES = (
    ("ConcurrencyPrefs", "minis_concurrency_prefs"),
    ("AgentRuntimeLimitsPrefs", "minis_runtime_limits_prefs"),
    ("FastModePrefs", "minis_fast_mode_prefs"),
)
# Read from the RAW text: _strip_comments_and_strings blanks string contents.
_ARCH02_DECL_LITS = {
    lit: re.compile(r'\bconst\s+val\s+\w+\s*=\s*"%s"' % re.escape(lit))
    for _obj, lit in _ARCH02_CACHES
}
_ARCH02_PRIME_FUN_RE = re.compile(
    r"\bfun\s+prime\s*\(\s*\w+\s*:\s*(?:android\.content\.)?Context\b")
_ARCH02_CACHED_FIELD_RE = re.compile(
    r"@Volatile\b(?:\s+@\w+)*\s*private\s+var\s+(cached\w*)")
_ARCH02_LISTENER_DECL_RE = re.compile(
    r"\bval\s+(\w+)\s*=\s*(?:[\w.]+\.)?OnSharedPreferenceChangeListener\s*\{")
_ARCH02_CONTEXT_FIELD_RE = re.compile(
    r"\b(?:val|var)\s+(\w+)\s*:\s*(?:android\.content\.)?Context\b")


def _object_bodies(clean):
    """[(object name, brace body)] for every `object Name { ... }` in `clean`."""
    out = []
    for m in re.finditer(r"\bobject\s+(\w+)\s*(?::[^{]*?)?\{", clean):
        body = _brace_body(clean, m.end() - 1)
        out.append((m.group(1), body if body is not None else ""))
    return out


def _self_sync_defects(body, obj, rel, problems, report):
    """S1-S3 + C7 for one in-scope object body. Appends findings."""
    ctx_fields = _ARCH02_CONTEXT_FIELD_RE.findall(body)
    if ctx_fields:
        problems.append(
            "%s: `object %s` keeps a Context-typed field (%s) -- the self-sync listener "
            "must reload from the SharedPreferences the callback hands it; a retained "
            "Context is the F-235 dead field this refactor is meant to keep out"
            % (rel, obj, ", ".join(sorted(set(ctx_fields))))
        )

    decls = _ARCH02_LISTENER_DECL_RE.findall(body)
    if not decls:
        problems.append(
            "%s: `object %s` owns a primed cache but declares no object-owned "
            "`OnSharedPreferenceChangeListener` field -- prime() alone cannot keep the "
            "cache in step with a direct SharedPreferences write (minis-config / backup "
            "restore / the agent), so the context-free readers keep the startup snapshot"
            % (rel, obj)
        )
        return

    prime_m = _ARCH02_PRIME_FUN_RE.search(body)
    brace = body.find("{", prime_m.end()) if prime_m else -1
    prime_body = _brace_body(body, brace) if brace >= 0 else None
    if prime_body is None:
        problems.append(
            "%s: `object %s` exposes prime() but its body could not be located -- cannot "
            "prove the self-sync listener is registered" % (rel, obj)
        )
        return

    bodies = _listener_bodies(body)
    for ident in decls:
        if not re.search(
            r"\.registerOnSharedPreferenceChangeListener\s*\(\s*" + re.escape(ident) + r"\s*\)",
            prime_body,
        ):
            problems.append(
                "%s: `object %s` declares the self-sync listener `%s` but never registers "
                "it from prime() -- a listener that is never registered refreshes nothing"
                % (rel, obj, ident)
            )
            continue
        found = [b for n, b in bodies if n == ident]
        if not found:
            problems.append(
                "%s: `object %s` registers `%s` but its body could not be located -- cannot "
                "prove it refreshes the cache" % (rel, obj, ident)
            )
        elif not re.search(r"\bcached\w*\s*=(?!=)", found[0]) and "loadFromPreferences(" not in found[0]:
            problems.append(
                "%s: `object %s`'s self-sync listener `%s` refreshes nothing (no `cached… =` "
                "assignment and no shared loader call) -- it satisfies the shape while leaving "
                "the cache at the startup value, i.e. the same silent desync one layer in"
                % (rel, obj, ident)
            )
        else:
            report.append("  %s: `object %s` self-syncs via `%s`" % (rel, obj, ident))


def scan_self_priming(root):
    """ARCH-02 self-sync criteria. Returns (report lines, problems)."""
    report = []
    problems = []
    checked = set()
    files = []
    for path in _kt_files(root):
        raw = path.read_text(encoding="utf-8", errors="replace")
        files.append((path, raw, _strip_comments_and_strings(raw)))

    def check(path, clean, obj, body, why):
        rel = path.relative_to(root)
        if (str(rel), obj) in checked:
            return
        checked.add((str(rel), obj))
        _self_sync_defects(body, obj, rel, problems, report)

    # ── C4: cache identity anchor (the prefs file name literal) ──────────
    for obj, lit in _ARCH02_CACHES:
        decl_files = [f for f in files if _ARCH02_DECL_LITS[lit].search(f[1])]
        obj_declared = any(re.search(r"\bobject\s+%s\b" % obj, f[2]) for f in files)
        if not decl_files:
            if obj_declared:
                problems.append(
                    'no file declares the prefs file name "%s" although `object %s` still '
                    "exists -- the ARCH-02 identity anchor was renamed/removed, re-point "
                    "this criterion" % (lit, obj)
                )
            continue
        if not obj_declared:
            problems.append(
                'a file declares the prefs file name "%s" but no file declares `object %s` '
                "-- the object was renamed while the C4/C5 anchors did not follow"
                % (lit, obj)
            )
        for path, _raw, clean in decl_files:
            scoped = [(n, b) for n, b in _object_bodies(clean)
                      if _ARCH02_PRIME_FUN_RE.search(b) and _ARCH02_CACHED_FIELD_RE.search(b)]
            if not scoped:
                problems.append(
                    '%s declares the primed prefs file "%s" but exposes no `object` with '
                    "`fun prime(<x>: Context)` over a `@Volatile private var cached…` "
                    "snapshot -- the self-sync contract has no anchor here (shape renamed?)"
                    % (path.relative_to(root), lit)
                )
                continue
            for n, b in scoped:
                check(path, clean, n, b, "C4")

    # ── C5: shape anchor, name-independent (covers a future 4th cache) ───
    for path, _raw, clean in files:
        for n, b in _object_bodies(clean):
            if _ARCH02_PRIME_FUN_RE.search(b) and _ARCH02_CACHED_FIELD_RE.search(b):
                check(path, clean, n, b, "C5")

    if not checked:
        problems.append(
            "no object with a primed (`fun prime(context: Context)` + `@Volatile private "
            "var cached…`) snapshot was found -- the ARCH-02 criteria have nothing to check "
            "(did the prefs caches move or get renamed?)"
        )
    return report, problems


def _selftest():
    """Fixture mode: each fixture is a miniature tree with exactly one defect."""
    import subprocess
    import tempfile

    def tree(files):
        root = tempfile.mkdtemp(prefix="prefs-priming-selftest-")
        for rel, content in files.items():
            p = Path(root) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root

    def run(root):
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), root],
            capture_output=True, text=True,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    HOLD = ("import android.content.Context\n"
            "import android.content.SharedPreferences\n")

    def cfg(conc_prime=True, lim_prime=True):
        cp = "            ConcurrencyPrefs.prime(context)\n" if conc_prime else ""
        lp = "            AgentRuntimeLimitsPrefs.prime(context)\n" if lim_prime else ""
        return HOLD + (
            "object Cfg {\n"
            "    @Volatile private var concListener: SharedPreferences.OnSharedPreferenceChangeListener? = null\n"
            "    @Volatile private var limListener: SharedPreferences.OnSharedPreferenceChangeListener? = null\n"
            "    fun register(r: Any, context: Context) {\n"
            "        r.register(path = %s)\n"
            "        val prefs = context.getSharedPreferences(ConcurrencyPrefs.PREFS, Context.MODE_PRIVATE)\n"
            "        val cl = SharedPreferences.OnSharedPreferenceChangeListener { _, _ ->\n"
            "%s"
            "        }\n"
            "        concListener = cl\n"
            "        prefs.registerOnSharedPreferenceChangeListener(cl)\n"
            "        val limits = context.getSharedPreferences(AgentRuntimeLimitsPrefs.PREFS, Context.MODE_PRIVATE)\n"
            "        val ll = SharedPreferences.OnSharedPreferenceChangeListener { _, _ ->\n"
            "%s"
            "        }\n"
            "        limListener = ll\n"
            "        limits.registerOnSharedPreferenceChangeListener(ll)\n"
            "    }\n"
            "}\n" % (_PRIMING_CFG_PATH, cp, lp)
        )

    def screen(prime_first=True):
        if prime_first:
            body = ("            ConcurrencyPrefs.prime(app)\n"
                    "            maxSessions = ConcurrencyPrefs.maxConcurrentSessions()\n")
        else:
            body = ("            maxSessions = ConcurrencyPrefs.maxConcurrentSessions()\n"
                    "            ConcurrencyPrefs.prime(app)\n")
        return HOLD + (
            "class Screen(private val context: Context) {\n"
            "    @Volatile private var listener: SharedPreferences.OnSharedPreferenceChangeListener? = null\n"
            "    var maxSessions: Int = 0\n"
            "    fun wire() {\n"
            "        val app = context.applicationContext\n"
            "        val concurrencyPrefs = app.getSharedPreferences(ConcurrencyPrefs.PREFS, Context.MODE_PRIVATE)\n"
            "        val l = SharedPreferences.OnSharedPreferenceChangeListener { _, _ ->\n"
            "%s"
            "        }\n"
            "        listener = l\n"
            "        concurrencyPrefs.registerOnSharedPreferenceChangeListener(l)\n"
            "    }\n"
            "}\n" % body
        )

    inline_reg = HOLD + (
        "object Cfg {\n"
        "    fun register(r: Any, context: Context) {\n"
        "        r.register(path = %s)\n"
        "        val prefs = context.getSharedPreferences(ConcurrencyPrefs.PREFS, Context.MODE_PRIVATE)\n"
        "        prefs.registerOnSharedPreferenceChangeListener { _, _ ->\n"
        "            ConcurrencyPrefs.prime(context)\n"
        "        }\n"
        "    }\n"
        "}\n" % _PRIMING_CFG_PATH
    )

    renamed = HOLD + (
        "object MinisApp {\n"
        "    @Volatile private var listener: SharedPreferences.OnSharedPreferenceChangeListener? = null\n"
        "    fun onCreate(context: Context) {\n"
        "        ConcurrencyPrefs.prime(context)\n"
        "        val prefs = context.getSharedPreferences(\"other_prefs\", Context.MODE_PRIVATE)\n"
        "        val l = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> }\n"
        "        listener = l\n"
        "        prefs.registerOnSharedPreferenceChangeListener(l)\n"
        "    }\n"
        "}\n"
    )

    def self_sync_src(lit="minis_concurrency_prefs", obj="ConcurrencyPrefs",
                      decl=True, register=True, refresh=True, context_field=False):
        """A miniature primed-cache object in the shipped shape, broken in one way."""
        lines = [
            HOLD,
            "object %s {\n" % obj,
            '    const val PREFS = "%s"\n' % lit,
            "    const val KEY = \"maxConcurrentSessions\"\n",
            # shipped shape: @Volatile and the field live on separate lines
            "    @Volatile\n    private var cachedValue = 1\n",
        ]
        if context_field:
            lines.append("    private var appContext: Context? = null\n")
        if decl:
            if refresh:
                lines.append(
                    "    private val selfSyncListener =\n"
                    "        SharedPreferences.OnSharedPreferenceChangeListener { changed, _ ->\n"
                    "            cachedValue = changed.getInt(KEY, 1)\n"
                    "        }\n"
                )
            else:
                lines.append(
                    "    private val selfSyncListener =\n"
                    "        SharedPreferences.OnSharedPreferenceChangeListener { _, _ ->\n"
                    "        }\n"
                )
        lines.append(
            "    fun prime(context: Context) {\n"
            "        val p = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)\n"
        )
        if register and decl:
            lines.append("        p.registerOnSharedPreferenceChangeListener(selfSyncListener)\n")
        lines.append(
            "        cachedValue = p.getInt(KEY, 1)\n"
            "    }\n"
            "}\n"
        )
        return "".join(lines)

    fixtures = [
        # name, files, expected rc, fragment that must appear ("" = must be green)
        ("clean tree (precision control)",
         {"Cfg.kt": cfg(), "Screen.kt": screen(), "Prefs.kt": self_sync_src()},
         0, "re-primed by"),
        ("concurrency listener never primes (C1)",
         {"Cfg.kt": cfg(conc_prime=False), "Screen.kt": screen()},
         1, "re-primes nothing: the listener `cl` on ConcurrencyPrefs.PREFS"),
        ("limits listener never primes (C2)",
         {"Cfg.kt": cfg(lim_prime=False), "Screen.kt": screen()},
         1, "re-primes nothing: the listener `ll` on AgentRuntimeLimitsPrefs.PREFS"),
        ("listener reads the cap before priming (C3)",
         {"Cfg.kt": cfg(), "Screen.kt": screen(prime_first=False)},
         1, "reads ConcurrencyPrefs.maxConcurrentSessions() before priming"),
        ("inline registration on the concurrency handle (fail-closed)",
         {"Cfg.kt": inline_reg}, 1, "no resolvable change-listener registration"),
        ("config-path anchor renamed away (fail-closed)",
         {"MinisApp.kt": renamed}, 1, "the anchor was renamed/removed"),
        # [ARCH-02] self-sync fixtures -- one defect each.
        ("primed cache declares no self-sync listener (S1/C4)",
         {"Prefs.kt": self_sync_src(decl=False)}, 1,
         "declares no object-owned"),
        ("self-sync listener never registered from prime (S2)",
         {"Prefs.kt": self_sync_src(register=False)}, 1,
         "never registers"),
        ("self-sync listener refreshes nothing (S3)",
         {"Prefs.kt": self_sync_src(refresh=False)}, 1,
         "refreshes nothing"),
        ("self-synced cache object (precision control, green)",
         {"Prefs.kt": self_sync_src(), "Cfg.kt": cfg(), "Screen.kt": screen()},
         0, "self-syncs via"),
        ("primed object with no cached snapshot in it (C4 anchor lost)",
         {"Prefs.kt": HOLD + (
             "object Prefs {\n"
             '    const val PREFS = "minis_concurrency_prefs"\n'
             "    fun prime(context: Context) {\n"
             "    }\n"
             "}\n")}, 1,
         "no `object` with `fun prime"),
        ("object exists but its prefs-file literal was renamed (C6a)",
         {"Prefs.kt": self_sync_src(lit="renamed_concurrency_prefs")}, 1,
         'no file declares the prefs file name "minis_concurrency_prefs"'),
        ("primed cache object retains a Context field (C7)",
         {"Prefs.kt": self_sync_src(context_field=True)}, 1,
         "keeps a Context-typed field"),
    ]

    ok = True
    for name, files, want_rc, fragment in fixtures:
        root = tree(files)
        rc, out = run(root)
        good = True
        if want_rc == 0:
            good = rc == 0
        else:
            good = rc != 0
        if fragment and fragment not in out:
            good = False
        if want_rc == 0 and "Re-priming" not in out:
            # a green fixture must prove the criteria actually ran
            good = False
        print("  %s %s" % ("PASS" if good else "FAIL", name))
        if not good:
            ok = False
            print("    expected rc=%s, got rc=%s; wanted fragment %r" % (want_rc, rc, fragment))
            print("    output: %s" % out.strip().replace("\n", "\n    ")[:1200])
    if not ok:
        print("PREFS_PRIMING_SELFTEST=FAIL")
        return 1
    print("PREFS_PRIMING_SELFTEST=PASS")
    return 0


if "--selftest" in sys.argv:
    sys.exit(_selftest())

for path in sorted(ROOT.rglob("*.kt")):
    sp = str(path)
    if "/build/" in sp or "/test/" in sp:
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    if "registerOnSharedPreferenceChangeListener" not in text:
        continue
    lines = text.split("\n")
    rel = path.relative_to(ROOT)
    fields = field_declarations(text)

    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if not REGISTER_RE.search(ln):
            continue
        # `override fun registerOnSharedPreferenceChangeListener(...) {}` is the
        # no-op stub in EncryptedPrefsFactory, not a call site.
        if re.search(r"\bfun\s+registerOnSharedPreferenceChangeListener\b", ln):
            continue
        # `unregisterOnSharedPreferenceChangeListener` contains the same suffix.
        if re.search(r"\bunregisterOnSharedPreferenceChangeListener\b", ln):
            continue

        ctx = "\n".join(lines[max(0, i - 2):i + 1])
        if ESCAPE_RE.search(ctx):
            sites.append((str(rel), i + 1, "escaped"))
            continue

        # Inline lambda argument?
        if INLINE_RE.search(ln):
            problems.append(
                f"{rel}:{i+1} registers an inline lambda with no strong holder -- "
                f"SharedPreferencesImpl holds listeners in a WeakHashMap, so the GC "
                f"collects it and the callback silently stops firing"
            )
            sites.append((str(rel), i + 1, "INLINE (hazard)"))
            continue

        # Otherwise: expect an identifier argument, and require it be held.
        m = re.search(r"\.registerOnSharedPreferenceChangeListener\s*\(\s*([\w.]+)", ln)
        if not m:
            problems.append(f"{rel}:{i+1} could not parse the listener argument")
            sites.append((str(rel), i + 1, "unparsed"))
            continue
        ident = m.group(1).split(".")[-1]
        if ident in fields:
            sites.append((str(rel), i + 1, f"held ({ident})"))
        elif ref_count(text, ident) >= 2 and stored_into_something(text, ident):
            # Declared as a local, but also assigned into another holder.
            sites.append((str(rel), i + 1, f"held via assignment ({ident})"))
        elif inside_on_dispose(text, ident):
            # Compose: the onDispose lambda captures it for the effect's life.
            sites.append((str(rel), i + 1, f"held via onDispose ({ident})"))
        else:
            problems.append(
                f"{rel}:{i+1} registers `{ident}` but nothing keeps it alive past "
                f"the registering frame (no field holds it) -- the GC collects it "
                f"and the callback silently stops firing"
            )
            sites.append((str(rel), i + 1, f"UNHELD ({ident})"))

_priming_report, _priming_problems = scan_priming(ROOT)
if _priming_report:
    print("Re-priming ([fix/da1026-prefs-priming] cached values refreshed on external writes):")
    for _line in _priming_report:
        print(_line)
    print("")

_arch02_report, _arch02_problems = scan_self_priming(ROOT)
if _arch02_report:
    print("Self-syncing primed caches ([ARCH-02] the cache owns its own re-priming listener):")
    for _line in _arch02_report:
        print(_line)
    print("")

print("SharedPreferences change-listener registrations:")
for rel, line, kind in sites:
    print(f"  {rel}:{line}  {kind}")

if not sites:
    problems.append(
        "found no registerOnSharedPreferenceChangeListener call sites at all -- "
        "the call may have been renamed; re-point this guard if so"
    )

if problems:
    print("\nFAIL: a prefs change listener may be garbage-collected")
    for p in problems:
        print(f"  - {p}")
    print("\n  Fix: bind the listener to a name and keep it in a field, e.g.")
    print("       private val prefsListener = OnSharedPreferenceChangeListener { _, _ -> ... }")
    print("       prefs.registerOnSharedPreferenceChangeListener(prefsListener)")
    print("  Or justify inline with `prefs-listener-ok: <reason>`.")

if _priming_problems:
    print("\nFAIL: a cached runtime prefs value is never re-primed after an external write")
    for p in _priming_problems:
        print(f"  - {p}")
    print("\n  Fix: where the config path is registered, also register a listener on that")
    print("       prefs file and have it call <Prefs>.prime(context) before any cached read.")

if _arch02_problems:
    print("\nFAIL: a primed cache is not self-syncing ([ARCH-02] contract broken)")
    for p in _arch02_problems:
        print(f"  - {p}")
    print("\n  Fix: in the object that owns the cache, declare a strongly-held listener")
    print("       (OnSharedPreferenceChangeListener { changed, _ -> <reload> }) and register")
    print("       it once from prime(). See scripts/scan/prefs_listener_holder_guard.py [ARCH-02].")

if problems or _priming_problems or _arch02_problems:
    sys.exit(1)

print(f"\nOK: all {len(sites)} prefs listener registration(s) strongly held")
print(f"OK: re-priming criteria green ({len(_priming_report)} re-priming site(s) verified)")
print(f"OK: self-syncing primed caches ({len(_arch02_report)} cache(s) own their re-priming listener)")
