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
#   C3g [dv17-b07] the C3 rule generalized to EVERY primed cache: the
#       (object -> no-arg cached getters) table is derived from the tree
#       itself (same ARCH-02 primed-cache predicate), and every
#       change-listener body that reads one of those getters must prime that
#       object first. B-07 shipped exactly the hard-coded pair's blind spot:
#       a listener reading AgentRuntimeLimitsPrefs.compactModelEntryId()
#       without priming was invisible to C3.
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

    # ── C3 generalized ([dv17-b07]): every primed cache, every cached read ──
    # C3 above hard-codes ONE (object, getter) pair (ConcurrencyPrefs +
    # maxConcurrentSessions). B-07 shipped exactly that blind spot: the
    # ModelGroupsScreen listener read AgentRuntimeLimitsPrefs.compactModelEntryId()
    # (a @Volatile-cache getter like any other) without priming first, and the
    # criterion never fired. The generalized version derives the (object,
    # no-arg cached getters) table from the tree itself -- same ARCH-02
    # predicate the self-sync criteria already use -- so a listener body that
    # reads ANY cached getter of ANY primed cache must prime that cache first.
    # A getter "derives" a cached field in either shape this codebase uses:
    #     fun compactModelEntryId(): String = cachedCompactModelEntryId
    #     fun isEnabled(): Boolean { return cachedEnabled }
    # Drift posture: content-anchored. A tree whose listeners read no cached
    # getter is simply out of scope (no fail-closed miss) -- the anchors live
    # in the listener bodies themselves, and trees without a prefs world
    # (mini fixtures) never enter the loop.
    getter_re = re.compile(r"\bfun\s+(\w+)\s*\(\s*\)\s*:\s*[^\n{=]*=\s*(cached\w*)")
    getter_re_block = re.compile(
        r"\bfun\s+(\w+)\s*\(\s*\)\s*:[^\n{]*\{[^{}]*?\breturn\s+(cached\w*)")
    # Pass 1: build the (object -> no-arg cached getters) table tree-wide. The
    # listener that reads a cached getter almost never lives in the file that
    # declares the object, so per-file tables would always be empty there.
    obj_getters = {}
    for path, _raw, clean in files:
        for obj, body in _object_bodies(clean):
            if not (_ARCH02_PRIME_FUN_RE.search(body) and _ARCH02_CACHED_FIELD_RE.search(body)):
                continue
            fields = set(_ARCH02_CACHED_FIELD_RE.findall(body))
            for m in getter_re.finditer(body):
                if m.group(2) in fields:
                    obj_getters.setdefault(obj, set()).add(m.group(1))
            for m in getter_re_block.finditer(body):
                if m.group(2) in fields:
                    obj_getters.setdefault(obj, set()).add(m.group(1))
    if obj_getters:
        report.append("  cached-getter table: %s" % ", ".join(
            "%s(%d getters)" % (o, len(g)) for o, g in sorted(obj_getters.items())))
    cache_reads = 0
    for path, _raw, clean in files:
        rel = path.relative_to(root)
        for name, body in _listener_bodies(clean):
            for obj, getters in sorted(obj_getters.items()):
                reads = ["%s.%s()" % (obj, g) for g in sorted(getters)
                         if ("%s.%s()" % (obj, g)) in body]
                if not reads:
                    continue
                cache_reads += 1
                first_at = min(body.find(r) for r in reads)
                prime_at = body.find("%s.prime(" % obj)
                if prime_at < 0 or prime_at > first_at:
                    problems.append(
                        "%s: the listener `%s` reads %s before priming the cache -- the read "
                        "can serve the value from before the write that triggered the "
                        "callback (SharedPreferencesImpl dispatch order is unspecified, so "
                        "this listener can run before %s' own self-sync listener)"
                        % (rel, name or "<inline>", ", ".join(reads), obj)
                    )
                else:
                    report.append("  %s: listener primes before reading %s" % (rel, obj))
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

    # Drift detector, NOT a shape requirement: only speak up when the tree still
    # carries an ARCH-02 anchor (one of the three cache objects by name, or one
    # of their prefs-file literals) while no primed-cache object is recognisable.
    #
    # It must stay silent on a tree with no prefs-cache world at all -- the
    # miniature fixture trees used by test_scan.py / --selftest are exactly that,
    # and an unconditional "nothing to check" there turns 4 green-expectation
    # fixtures red (found by running the frozen test_scan.py on this branch).
    anchored = any(
        re.search(r"\bobject\s+(%s)\b" % "|".join(n for n, _l in _ARCH02_CACHES), clean)
        or any(_ARCH02_DECL_LITS[lit].search(raw) for _o, lit in _ARCH02_CACHES)
        for _path, raw, clean in files
    )
    if not checked and anchored:
        problems.append(
            "the tree still declares a primed prefs cache object / prefs-file literal but no "
            "object with the primed shape (`fun prime(<x>: Context)` over a `@Volatile private "
            "var cached…` snapshot) was found -- the ARCH-02 criteria have lost their anchor "
            "(did the caches move or get renamed?)"
        )
    return report, problems


# ==========================================================================
# [dv22-b1] Batch-save screens: key-filtered listeners + snapshot Save.
#
# Concern: a Settings screen that (a) backs its controls with Compose state and
# (b) re-persists EVERY knob it shows in one batched Save. AOSP
# `SharedPreferencesImpl.apply()` notifies the registered listeners
# SYNCHRONOUSLY when called on the main thread (`notifyListeners` runs the
# callbacks inline before `apply` returns -- verified against AOSP android
# main `SharedPreferencesImpl.java`, `:519 Okay to notify the listeners before
# it's hit disk`). So inside a Save onClick the FIRST write's callback runs
# before the later write calls evaluate their arguments. Two defects follow:
#
#   * an UNKEYED listener body re-reads every control from prefs, so the
#     first apply() resets the in-memory edit buffer to the on-disk values
#     mid-Save, and the later writes persist the RESET values -- Save itself
#     silently rolls the user's own edits back (RuntimeLimitsScreen pre-fix:
#     40 states reset by `SubagentPrefs.setEnabled`'s own apply).
#   * even a KEYED listener that re-reads the whole group under one membership
#     test (`key in ALL_KEYS`) clears every unsaved edit buffer when an
#     external single-key write lands (ChatTuningScreen pre-fix: 7 buffers).
#
# Criteria (the scope is DERIVED from the tree, never a path list):
#   C8   in a file that BOTH declares an `OnSharedPreferenceChangeListener` and
#        calls a `<x>Prefs.save(` whose argument list (nested spans included)
#        carries >= 2 named arguments (a batch write), every listener body that
#        assigns >= 2 distinct identifiers must make a key decision:
#        `when (key)` / `key in ...` / `if (key == ...)`.
#   C8b  the key decision must be PER KEY: the `when (key)` arm count must be
#        >= the number of distinct identifiers the listener body assigns (a
#        `key in` membership form counts as ONE arm). One arm per control is
#        the AppearanceScreen shape; a whole-group re-read under one test
#        still destroys the other edit buffers.
#   C9   inside such a screen's `onClick = { ... }` blocks, every prefs write
#        call AFTER the first one must not take a listener-assigned identifier
#        as a bare argument expression -- the first apply() has already reset
#        those identifiers by the time the later arguments evaluate, so the
#        write persists the RESET value. The values must come from locals
#        captured before the first write (the `val pending... = ...` shape).
#        The FIRST write call is exempt: its arguments evaluate before its own
#        apply() fires, so direct state reads are safe there (and a screen
#        whose Save is a single call is safe by construction -- evaluation
#        order, exactly the ChatTuningScreen save path).
#
# Drift: if NO file qualifies as a batch-save screen while the tree still holds
# Compose state (`mutableStateOf(`) over Prefs getters behind a change
# listener, the batch-save shape was renamed/removed -- re-point this
# criterion.
#
# Escape hatch: `prefs-listener-ok: <reason>` anywhere in the file exempts the
# whole file from C8/C8b/C9 (the exemption is reported, not silent).
# ==========================================================================

_B1_BULK_SAVE_RE = re.compile(r"\b\w*Prefs\.save\s*\(")
_B1_KEYED_RE = re.compile(
    r"\bwhen\s*\(\s*key\s*\)|\bkey\s+in\b|\bif\s*\(\s*key\s*==")
_B1_WRITE_RE = re.compile(r"\b\w*Prefs\.(?:save|setEnabled|setMax\w+)\s*\(")
_B1_ONCLICK_RE = re.compile(r"\bonClick\s*=\s*\{")
_B1_ARG_IDENT_RE = re.compile(r"(?<![\w.])([a-zA-Z_]\w*)\s*(?=\s*[,)])")
_B1_ASSIGN_RE = re.compile(r"(?<![\w.])([a-zA-Z_]\w*)\s*=(?!=)")

def _paren_span(text, open_idx):
    """Text between the '(' at open_idx and its match (None when unbalanced)."""
    depth, i, n = 1, open_idx + 1, len(text)
    while i < n and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    if depth:
        return None
    return text[open_idx + 1:i - 1]

def _b1_when_arms(body):
    """`when (key) { ... }` arm count, at brace depth 0 of the when body.

    Returns None when the body has no `when (key)` (the caller then treats any
    other key decision -- `key in ...` -- as a single arm), and 0 when the
    `when (key)` exists but its body cannot be balanced (fail-closed: a broken
    when is a red, not a silent skip).
    """
    m = re.search(r"\bwhen\s*\(\s*key\s*\)\s*\{", body)
    if not m:
        return None
    inner = _brace_body(body, m.end() - 1)
    if inner is None:
        return 0
    arms, depth, i, n = 0, 0, 0, len(inner)
    while i < n:
        c = inner[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if depth == 0 and inner.startswith("->", i):
            arms += 1
            i += 2
            continue
        i += 1
    return arms

def scan_batch_save_screens(root):
    """[dv22-b1] C8/C8b/C9. Returns (report lines, problems)."""
    report = []
    problems = []
    files = []
    for path in _kt_files(root):
        raw = path.read_text(encoding="utf-8", errors="replace")
        files.append((path, raw, _strip_comments_and_strings(raw)))

    scope = []
    for path, raw, clean in files:
        if "OnSharedPreferenceChangeListener" not in clean:
            continue
        if ESCAPE_RE.search(raw):
            report.append(
                "  %s: exempted from [dv22-b1] by the prefs-listener-ok hatch"
                % path.relative_to(root))
            continue
        for m in _B1_BULK_SAVE_RE.finditer(clean):
            span = _paren_span(clean, m.end() - 1)
            if span and len(_B1_ASSIGN_RE.findall(span)) >= 2:
                scope.append((path, raw, clean))
                break

    for path, raw, clean in scope:
        rel = path.relative_to(root)
        # C8/C8b: every multi-assign listener body decides by key, per key.
        for name, body in _listener_bodies(clean):
            idents = set(_B1_ASSIGN_RE.findall(body))
            if len(idents) < 2:
                # A single-control listener has nothing to protect.
                continue
            if not _B1_KEYED_RE.search(body):
                problems.append(
                    "%s: the listener `%s` re-reads %d controls without any key decision -- "
                    "apply() notifies listeners SYNCHRONOUSLY on the main thread, so this "
                    "screen's own Save resets every edit buffer mid-write and the later "
                    "write args persist the RESET values (decide by `key`: one when-arm per "
                    "control, the AppearanceScreen shape)"
                    % (rel, name or "<inline>", len(idents))
                )
            else:
                arms = _b1_when_arms(body)
                if arms is None:
                    arms = 1  # `key in ...` / `if (key == ...)` -- a single decision
                if arms < len(idents):
                    problems.append(
                        "%s: the listener `%s` decides by key but not PER key (when (key) "
                        "arms=%s, distinct reset identifiers=%s) -- a whole-group re-read "
                        "under one arm still clears every unsaved edit buffer when a single "
                        "key changes"
                        % (rel, name or "<inline>", arms, sorted(idents))
                    )
                else:
                    report.append(
                        "  %s: listener re-reads %d control(s) per key (%d when-arms)"
                        % (rel, len(idents), arms)
                    )

        # C9: after the first prefs write in an onClick, args must be snapshots.
        resettable = set()
        for _n, body in _listener_bodies(clean):
            resettable |= set(_B1_ASSIGN_RE.findall(body))
        for m in _B1_ONCLICK_RE.finditer(clean):
            body = _brace_body(clean, m.end() - 1)
            if body is None:
                continue
            writes = [wm.end() - 1 for wm in _B1_WRITE_RE.finditer(body)]
            for widx, wopen in enumerate(writes):
                if widx == 0:
                    # First write: its args evaluate before its own apply().
                    continue
                span = _paren_span(body, wopen)
                if span is None:
                    continue
                bare = sorted(set(_B1_ARG_IDENT_RE.findall(span)) & resettable)
                if bare:
                    problems.append(
                        "%s: a Save write call reads the listener-resettable state (%s) as a "
                        "direct argument AFTER an earlier apply() in the same onClick -- the "
                        "screen listener has already reset those values, so this write "
                        "persists the RESET value. Capture the pending values into locals "
                        "BEFORE the first write"
                        % (rel, ", ".join(bare[:8]) + (" ..." if len(bare) > 8 else ""))
                    )
            if len(writes) >= 2:
                report.append(
                    "  %s: Save writes %d prefs call(s) from a pre-captured snapshot"
                    % (rel, len(writes))
                )

    # Drift: scope lost while a prefs-backed Compose screen still exists.
    if not scope:
        anchored = any(
            "OnSharedPreferenceChangeListener" in clean
            and "mutableStateOf(" in clean
            and "Prefs" in clean
            for _p, _r, clean in files
        )
        if anchored:
            problems.append(
                "no screen qualifies as a batch-save screen although the tree still holds "
                "Compose state over Prefs getters behind a change listener -- the batch "
                "`<x>Prefs.save(` call was renamed/removed, re-point the [dv22-b1] criteria"
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

    def limits_tree(prime_first=True):
        """[dv17-b07] A second primed cache (AgentRuntimeLimitsPrefs shape) with a
        screen listener reading a NON-cap cached getter -- the exact blind spot
        B-07 shipped: the old C3 only ever checked ConcurrencyPrefs +
        maxConcurrentSessions(). The object must satisfy the ARCH-02 self-sync
        criteria so the fixture is otherwise clean."""
        read = "            compactId = AgentRuntimeLimitsPrefs.compactModelEntryId()\n"
        prime = "            AgentRuntimeLimitsPrefs.prime(app)\n" if prime_first else ""
        return HOLD + (
            "object AgentRuntimeLimitsPrefs {\n"
            '    const val PREFS = "minis_runtime_limits_prefs"\n'
            '    const val KEY_COMPACT_MODEL_ENTRY_ID = "compactModelEntryId"\n'
            "    @Volatile\n"
            '    private var cachedCompactModelEntryId: String = ""\n'
            "    private val selfSyncListener =\n"
            "        SharedPreferences.OnSharedPreferenceChangeListener { changed, _ ->\n"
            '            cachedCompactModelEntryId = changed.getString(KEY_COMPACT_MODEL_ENTRY_ID, "") ?: ""\n'
            "        }\n"
            "    fun compactModelEntryId(): String = cachedCompactModelEntryId\n"
            "    fun prime(context: Context) {\n"
            "        val p = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)\n"
            "        p.registerOnSharedPreferenceChangeListener(selfSyncListener)\n"
            '        cachedCompactModelEntryId = p.getString(KEY_COMPACT_MODEL_ENTRY_ID, "") ?: ""\n'
            "    }\n"
            "}\n"
            "class LimitsScreen(private val context: Context) {\n"
            '    var compactId: String = ""\n'
            "    fun wire() {\n"
            "        val app = context.applicationContext\n"
            "        val prefs = app.getSharedPreferences(AgentRuntimeLimitsPrefs.PREFS, Context.MODE_PRIVATE)\n"
            "        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->\n"
            "            if (key == null || key == AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID) {\n"
            + prime + read +
            "            }\n"
            "        }\n"
            "        prefs.registerOnSharedPreferenceChangeListener(listener)\n"
            "        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }\n"
            "    }\n"
            "}\n"
        )

    def bulk_screen(keyed="when", snapshot=True):
        """[dv22-b1] A miniature batch-save Settings screen. `keyed` picks the
        listener shape: "when" (per-key arms), "group" (one membership test
        re-reading the whole group), "none" (unkeyed). `snapshot` picks the
        Save shape: locals captured before the first write vs. direct state
        reads in the later write calls."""
        if keyed == "when":
            body = (
                "            when (key) {\n"
                '                "subagent.enabled" -> subEnabled = prefs.getBoolean(key, false)\n'
                '                "maxConcurrentSessions" -> maxSessions = ConcurrencyPrefs.maxConcurrentSessions()\n'
                '                "maxTurns" -> maxTurns = AgentRuntimeLimitsPrefs.maxTurns()\n'
                '                "guestTmpMaxAgeMin" -> guestTmpMaxAgeMin = AgentRuntimeLimitsPrefs.guestTmpMaxAgeMin()\n'
                "            }\n"
            )
        elif keyed == "group":
            body = (
                "            if (key in ALL_KEYS) {\n"
                "                subEnabled = prefs.getBoolean(key, false)\n"
                "                maxSessions = ConcurrencyPrefs.maxConcurrentSessions()\n"
                "                maxTurns = AgentRuntimeLimitsPrefs.maxTurns()\n"
                "                guestTmpMaxAgeMin = AgentRuntimeLimitsPrefs.guestTmpMaxAgeMin()\n"
                "            }\n"
            )
        else:
            body = (
                "            subEnabled = prefs.getBoolean(key, false)\n"
                "            maxSessions = ConcurrencyPrefs.maxConcurrentSessions()\n"
                "            maxTurns = AgentRuntimeLimitsPrefs.maxTurns()\n"
                "            guestTmpMaxAgeMin = AgentRuntimeLimitsPrefs.guestTmpMaxAgeMin()\n"
            )
        if snapshot:
            save = (
                "            val pendingSub = subEnabled\n"
                "            val pendingMax = maxSessions\n"
                "            val pendingTurns = maxTurns\n"
                "            val pendingTmp = guestTmpMaxAgeMin\n"
                "            SubagentPrefs.setEnabled(context, pendingSub)\n"
                "            ConcurrencyPrefs.setMaxConcurrentSessions(context, pendingMax)\n"
                "            AgentRuntimeLimitsPrefs.save(context = context, maxTurns = pendingTurns, guestTmpMaxAgeMin = pendingTmp)\n"
            )
        else:
            save = (
                "            SubagentPrefs.setEnabled(context, subEnabled)\n"
                "            ConcurrencyPrefs.setMaxConcurrentSessions(context, maxSessions)\n"
                "            AgentRuntimeLimitsPrefs.save(context = context, maxTurns = maxTurns, guestTmpMaxAgeMin = guestTmpMaxAgeMin)\n"
            )
        return HOLD + (
            "class BulkScreen(private val context: Context) {\n"
            "    var subEnabled: Boolean = false\n"
            "    var maxSessions: Int = 2\n"
            "    var maxTurns: Int = 30\n"
            "    var guestTmpMaxAgeMin: Int = 60\n"
            "    fun wire() {\n"
            "        val app = context.applicationContext\n"
            "        val prefs = app.getSharedPreferences(\"minis_runtime_limits_prefs\", Context.MODE_PRIVATE)\n"
            "        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->\n"
            + body +
            "        }\n"
            "        prefs.registerOnSharedPreferenceChangeListener(listener)\n"
            "        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }\n"
            "    }\n"
            "    fun save() {\n"
            "        onClick = {\n"
            + save +
            "        }\n"
            "    }\n"
            "}\n"
        )

    single_key_screen = HOLD + (
        "class PinScreen(private val context: Context) {\n"
        '    var compactId: String = ""\n'
        "    var other: Int = 0\n"
        "    fun wire() {\n"
        "        val prefs = context.applicationContext.getSharedPreferences(\"minis_runtime_limits_prefs\", Context.MODE_PRIVATE)\n"
        "        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->\n"
        '            compactId = prefs.getString(key, "") ?: ""\n'
        "            other = prefs.getInt(key, 0)\n"
        "        }\n"
        "        prefs.registerOnSharedPreferenceChangeListener(listener)\n"
        "        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }\n"
        "    }\n"
        "    fun save() {\n"
        "        onClick = {\n"
        "            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = compactId)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )

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
        # [dv17-b07] generalized C3: non-cap cached getter of a second primed cache.
        ("listener reads a non-cap cached getter with prime first (C3g)",
         {"Prefs.kt": limits_tree(prime_first=True)},
         0, "listener primes before reading AgentRuntimeLimitsPrefs"),
        ("listener reads a non-cap cached getter before priming (C3g)",
         {"Prefs.kt": limits_tree(prime_first=False)},
         1, "reads AgentRuntimeLimitsPrefs.compactModelEntryId() before priming"),
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
        # [dv22-b1] batch-save screen criteria.
        ("batch-save screen: keyed listener + snapshot Save (C8/C9 control)",
         {"Cfg.kt": cfg(), "Screen.kt": screen(), "Prefs.kt": self_sync_src(),
          "Bulk.kt": bulk_screen(keyed="when", snapshot=True)},
         0, "re-reads 4 control(s) per key"),
        ("batch-save listener has no key decision (C8)",
         {"Prefs.kt": self_sync_src(), "Bulk.kt": bulk_screen(keyed="none", snapshot=True)},
         1, "without any key decision"),
        ("batch-save listener re-reads the whole group under one membership test (C8b)",
         {"Prefs.kt": self_sync_src(), "Bulk.kt": bulk_screen(keyed="group", snapshot=True)},
         1, "decides by key but not PER key"),
        ("Save reads listener-resettable state after an earlier apply (C9)",
         {"Prefs.kt": self_sync_src(), "Bulk.kt": bulk_screen(keyed="when", snapshot=False)},
         1, "as a direct argument AFTER an earlier apply()"),
        ("single-key write screen stays out of C8 scope (precision control)",
         {"Cfg.kt": cfg(), "Screen.kt": screen(), "Prefs.kt": self_sync_src(),
          "Pin.kt": single_key_screen},
         0, "self-syncs via"),
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

_b1_report, _b1_problems = scan_batch_save_screens(ROOT)
if _b1_report:
    print("Batch-save screens ([dv22-b1] key-filtered listeners + snapshot Save):")
    for _line in _b1_report:
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

if _b1_problems:
    print("\nFAIL: a batch-save screen resets its edit buffers or reads them back mid-Save ([dv22-b1])")
    for p in _b1_problems:
        print(f"  - {p}")
    print("\n  Fix: decide by `key` in the listener (one when-arm per control, the")
    print("       AppearanceScreen shape) and capture every value Save persists into")
    print("       locals BEFORE the first write -- apply() notifies the listener")
    print("       synchronously, so later write args read the RESET state otherwise.")

if problems or _priming_problems or _arch02_problems or _b1_problems:
    sys.exit(1)

print(f"\nOK: all {len(sites)} prefs listener registration(s) strongly held")
print(f"OK: re-priming criteria green ({len(_priming_report)} re-priming site(s) verified)")
print(f"OK: self-syncing primed caches ({len(_arch02_report)} cache(s) own their re-priming listener)")
print(f"OK: batch-save screens green ({len(_b1_report)} [dv22-b1] verification line(s))")
