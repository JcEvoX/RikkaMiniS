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

    fixtures = [
        # name, files, expected rc, fragment that must appear ("" = must be green)
        ("clean tree (precision control)",
         {"Cfg.kt": cfg(), "Screen.kt": screen()}, 0, "re-primed by"),
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

if problems or _priming_problems:
    sys.exit(1)

print(f"\nOK: all {len(sites)} prefs listener registration(s) strongly held")
print(f"OK: re-priming criteria green ({len(_priming_report)} re-priming site(s) verified)")
