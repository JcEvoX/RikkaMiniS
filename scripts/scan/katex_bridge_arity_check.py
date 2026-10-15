#!/usr/bin/env python3
"""
KaTeX JS-bridge arity guard — the asset HTML and its Kotlin hosts must agree.

Bug class (audit3 BUG-05, live for ~5 weeks): `assets/katex/katex-render.html`
calls `AndroidBridge.onRendered(w, h, error, token)` — four arguments — while
`KaTeXView.kt` registered a THREE-argument `onRendered`. Chromium resolves
`@JavascriptInterface` members by name AND argument count, so the call never
reached the host: it died as "Method not found" inside the page, the callback
threw out of its `requestAnimationFrame`, `renderedBitmap` stayed null forever
and each uncached display formula kept its WebView alive. The sibling host
(`KatexWebViewPool`) had been updated in the same commit — one side of a
two-sided contract, updated once. The lesson ("JS bridge changes must be
verified on both sides") was written into the memory log and still did not
prevent this, because nothing mechanical looked at both files.

Criterion: for every `AndroidBridge.<method>(...)` call site in
`src/android/app/src/main/assets/**/*.html`, let N be its top-level argument
count. Every Kotlin file that registers a JS interface under that name (via
`addJavascriptInterface(..., "<name>")`) must declare `@JavascriptInterface
fun <method>(...)` with exactly N parameters. Files are checked individually —
a union across files would hide exactly the bug above (the pool's 4-parameter
declaration masked the view file's 3-parameter one).

Notes / limits:
  * Argument splitting tracks quotes and `()[]{}` nesting, not `<>`. Kotlin
    generics with a top-level comma would be miscounted — bridge signatures
    are primitives, keep them that way.
  * No escape hatch: the contract is purely mechanical (name + arity), and the
    rot guard below fails loudly if the asset tree or the registrations move.

Usage: python3 scripts/scan/katex_bridge_arity_check.py [repo_root]
Exit:  0 = every call site has a matching declaration, 1 = mismatch (or rot)
"""
import os
import re
import sys

IFACE_DEFAULT = "AndroidBridge"


def strip_comments(text):
    """Remove comments before any structural scan.

    Prose apostrophes in comments ("the pool's cache", "Chromium's bridge")
    otherwise read as string delimiters and swallow real parentheses — which is
    how the first draft of this scanner silently mis-parsed `KaTeXView.kt` and
    reported a clean tree. After stripping, a `'` in Kotlin is a char literal
    and a `'` in JS is a real string delimiter, matching the scanner's rules.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def match_paren(text, open_idx):
    """Index of the ')' matching the '(' at open_idx, or -1."""
    depth, quote, i = 0, None, open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_args(text):
    """Top-level comma split; returns [] for an empty list."""
    parts, depth, buf, quote, i = [], 0, "", None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf += ch
            if ch == "\\" and i + 1 < len(text):
                buf += text[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            buf += ch
        elif ch in "([{":
            depth += 1
            buf += ch
        elif ch in ")]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    parts.append(buf)
    parts = [p.strip() for p in parts]
    return [] if parts == [""] else parts


def registered_names(kt_text):
    """JS interface names this Kotlin file registers."""
    names = []
    for m in re.finditer(r"addJavascriptInterface\s*\(", kt_text):
        end = match_paren(kt_text, m.end() - 1)
        if end < 0:
            continue
        body = kt_text[m.end():end]
        literals = re.findall(r"\"([^\"]*)\"", body)
        if literals:
            names.append(literals[-1])
    return names


def declared_arities(kt_text):
    """{method: {arities}} for @JavascriptInterface fun declarations in a file.

    The annotation and the `fun` may sit on different lines (a KDoc block or a
    blank line in between), so search forward from each annotation rather than
    matching a single line.
    """
    out = {}
    sig = re.compile(r"\bfun\s+(\w+)\s*\(")
    for ann in re.finditer(r"@JavascriptInterface\b", kt_text):
        m = sig.search(kt_text, ann.end(), ann.end() + 400)
        if not m:
            continue
        end = match_paren(kt_text, m.end() - 1)
        if end < 0:
            continue
        out.setdefault(m.group(1), set()).add(len(split_args(kt_text[m.end():end])))
    return out


def js_call_sites(html_text, names):
    """[(iface, method, argc)] for every `<name>.<method>(...)` call in the page."""
    out = []
    pattern = r"\b(" + "|".join(re.escape(n) for n in names) + r")\.(\w+)\s*\("
    for m in re.finditer(pattern, html_text):
        end = match_paren(html_text, m.end() - 1)
        if end < 0:
            continue
        out.append((m.group(1), m.group(2), len(split_args(html_text[m.end():end]))))
    return out


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    assets = os.path.join(root, "src/android/app/src/main/assets")
    kotlin = os.path.join(root, "src/android/app/src/main/java")
    if not os.path.isdir(assets) or not os.path.isdir(kotlin):
        print(f"❌ ROT: expected {assets} and {kotlin} to exist")
        return 1

    hosts = {}
    unparsed = []
    for dirpath, _, files in os.walk(kotlin):
        for name in files:
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = strip_comments(fh.read())
            if "addJavascriptInterface" not in text:
                continue
            rel = os.path.relpath(path, root)
            names = registered_names(text)
            if not names:
                unparsed.append(rel)
                continue
            hosts[rel] = {n: declared_arities(text) for n in names}
    if unparsed:
        # A host that cannot be parsed must fail LOUDLY. This is not a
        # hypothetical: it was found by the guard's own mutation test — a
        # mangled registration made this file drop out of `hosts`, the other
        # host still supplied the interface name, and the guard went green on
        # a tree whose KaTeXView declaration no longer matched the page. That
        # is precisely the stale-host shape the guard exists to catch, so a
        # silent skip is the one failure mode it must never have.
        c = "❌"
        print(f"{c} ROT: addJavascriptInterface present but no interface name could be parsed:")
        for rel in unparsed:
            print(f"   {rel}")
        print("   Keep the interface name a string literal in the registration; if it")
        print("   must be computed, teach this guard how — do not let it skip the file.")
        return 1
    if not hosts:
        print("❌ ROT: no addJavascriptInterface registration found anywhere")
        return 1

    pages = []
    for dirpath, _, files in os.walk(assets):
        for name in files:
            if name.endswith((".html", ".htm")):
                pages.append(os.path.join(dirpath, name))

    all_names = {n for per_file in hosts.values() for n in per_file}
    calls_found = 0
    violations = []
    for path in sorted(pages):
        rel = os.path.relpath(path, root)
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = strip_comments(fh.read())
        for iface, method, argc in js_call_sites(text, all_names):
            calls_found += 1
            for host_file, per_name in sorted(hosts.items()):
                if iface not in per_name:
                    continue
                arities = per_name[iface].get(method)
                state = "OK" if arities and argc in arities else "MISMATCH"
                print(
                    f"  [{state}] {rel} → {iface}.{method}({argc} args)"
                    f" vs {host_file}: {sorted(arities) if arities else 'no declaration'}"
                )
                if state == "MISMATCH":
                    violations.append((rel, iface, method, argc, host_file, arities))
    if calls_found == 0:
        print("❌ ROT: no JS-bridge call site found in assets (contract unchecked)")
        return 1

    if violations:
        print("")
        print(f"❌ KATEX BRIDGE ARITY: {len(violations)} mismatch(es)")
        for rel, iface, method, argc, host_file, arities in violations:
            print(f"   {rel} calls {iface}.{method} with {argc} args, but {host_file} declares {sorted(arities) if arities else 'nothing'}")
        print("   Chromium resolves @JavascriptInterface by name AND arity: the call dies")
        print("   as \"Method not found\" inside the page and the host never runs.")
        return 1
    print(f"✅ KaTeX bridge arity: {calls_found} call site(s) matched in every host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
