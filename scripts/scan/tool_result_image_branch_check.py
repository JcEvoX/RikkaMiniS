#!/usr/bin/env python3
"""
Tool-result image consumer guard — every provider serializer that handles a
tool-result part must consume its image bytes.

Bug class (deep-audit-1017 ISSUE-01, P0): `read_image` / `browser_use`
screenshots reach the request as `AgentContentPart.ToolResult.imageData`, not as
an `ImageData` part. Anthropic (`AnthropicProvider.kt` -> image block inside
`tool_result.content`) and Gemini (`GeminiProvider.kt` -> sibling `inlineData`)
were fixed by "[FIX-1 / F-192]"; `OpenAIProvider.kt` never was — `grep -n
imageData` over that file returned **0 hits** while both siblings had the branch,
so every image a model read back through a tool call was invisible on OpenAI
Chat-Completions *and* Responses APIs. Classic "fixed 2 of 3" family recurrence:
the defect is invisible in review because the file looks complete, and it is
invisible at runtime because the request still succeeds (the model simply never
sees the bytes).

[deep-audit-1045 A-03] Sibling criterion, same family ("bytes that exist but
never reach the wire"): the three provider serializers each drop messages the
orphan-tool-pairing sanitizer emptied, using
`contentParts.isNotEmpty() || content.isNotEmpty()` — i.e. "is there TEXT?".
A `minis-model-use` turn carrying only an image or only an audio block has no
text, so the filter deleted the whole user turn and the payload never left the
device (the request stayed valid and semantically empty). The predicate now
lives once, in `MessagePayloadFilter.kt:hasWirePayload(...)`, and every
empty-message filter site must route through it. Criteria 5/6 below pin the
decision (does a media-only turn survive?) and the single source.

Criterion (structural — behaviour is pinned by `OpenAIToolResultImagesTest` and
`MessagePayloadFilterTest`):
  1. Every `*.kt` under the provider package that handles tool-result parts —
     either a `when` branch (`is AgentContentPart.ToolResult ->`) or a filter
     (`filterIsInstance<AgentContentPart.ToolResult>()`) — must reference
     `imageData` somewhere in the file. A provider that serializes tool results
     without ever looking at the bytes is the defect.
  2. Each individual `is AgentContentPart.ToolResult ->` branch body must itself
     reference `imageData`: a provider that handles one tool-result site and not
     the other (the shape OpenAI would have had if only its Chat path had been
     patched) fails here.
  3. Deliberate non-serializers live in EXEMPTIONS with (a) a written reason and
     (b) a `symbol` that must still exist in the file. A stale exemption — the
     file no longer handles tool results, or the cited symbol was renamed —
     is itself a failure, so an exemption cannot outlive the shape it excused.
  4. Scanning zero files is a hard failure (a path typo must not read as green).

Usage: python3 scripts/scan/tool_result_image_branch_check.py [repo_root]
       python3 scripts/scan/tool_result_image_branch_check.py --selftest
Exit:  0 = pass, 1 = violation (file:line printed)
"""
import os
import re
import shutil
import sys
import tempfile

PROVIDER_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app", "provider"
)

WHEN_BRANCH = "is AgentContentPart.ToolResult ->"
FILTER_CALL = "filterIsInstance<AgentContentPart.ToolResult>()"
IMAGE_FIELD = "imageData"

# [deep-audit-1045 A-03] The empty-message filter (derived from the tree: any
# `.filter {` / `.filterIndexed {` whose body asks `contentParts.isNotEmpty()`)
# must route through the shared payload predicate, and that predicate must weigh
# every media payload the providers can serialize.
# A site is either the delegated form (predicate call) or the legacy inline form
# (the marker the pre-fix tree used) — both are derived, so a reverted site is
# caught by the same criterion that a new site is covered by.
EMPTY_FILTER_SITE_MARKS = ("hasWirePayload(", "contentParts.isNotEmpty()")
PAYLOAD_PREDICATE_FILE = "MessagePayloadFilter.kt"
PAYLOAD_PREDICATE_DECL = "fun LLMMessage.hasWirePayload("
PAYLOAD_PREDICATE_CALL = "hasWirePayload("
PAYLOAD_MEDIA_ARMS = ("audioParts.isNotEmpty()", "imageParts.isNotEmpty()")
PAYLOAD_TURN_IMAGE_ARM = "turnImageCount > 0"
MIN_EMPTY_FILTER_SITES = 3

# file (provider-relative) -> {"reason": ..., "symbol": ...}
EXEMPTIONS = {
    "RequestSanitizers.kt": {
        "reason": (
            "sanitizeToolPairing only decides whether to KEEP or DROP whole parts "
            "(orphan tool_use / tool_result pairing) and never serializes bytes to "
            "any wire format, so there is nothing for it to consume"
        ),
        "symbol": "fun sanitizeToolPairing(",
    },
}


def strip_comments_and_strings(text):
    """Blank out comments and string contents so prose cannot fake a match and
    braces inside literals cannot unbalance a count."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if text.startswith('"""', i):
            i += 3
            while i < n and not text.startswith('"""', i):
                i += 1
            i += 3
            continue
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        out.append((i, c))
        i += 1
    return out


def branch_block(text, start):
    """Return the text of the block opened at/after `start` (first `{` reached),
    brace-balanced over code only."""
    code = strip_comments_and_strings(text)
    depth = 0
    opened = False
    end = None
    for idx, ch in code:
        if idx < start:
            continue
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                end = idx + 1
                break
    if end is None:
        return None
    return text[start:end]


def enclosing_block(text, target):
    """Return (start, block) of the innermost brace block containing [target],
    or (None, None). Braces inside comments/strings are ignored."""
    code = strip_comments_and_strings(text)
    stack = []
    for idx, ch in code:
        if idx >= target:
            break
        if ch == "{":
            stack.append(idx)
        elif ch == "}" and stack:
            stack.pop()
    if not stack:
        return (None, None)
    start = stack[-1]
    depth = 0
    opened = False
    for idx, ch in code:
        if idx < start:
            continue
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return (start, text[start:idx + 1])
    return (None, None)


def payload_filter_sites(text):
    """Derive the empty-message filter sites in one file: every brace block that
    opens a `.filter` / `.filterIndexed` lambda and either delegates to the
    shared predicate or still carries the legacy inline marker. No line numbers
    and no file list, so a new provider call site is covered the day it lands,
    and a site reverted to the inline predicate is caught by the same pass."""
    sites = []
    seen = set()
    for mark in EMPTY_FILTER_SITE_MARKS:
        for m in re.finditer(re.escape(mark), text):
            start, block = enclosing_block(text, m.start())
            if start is None or start in seen:
                continue
            head = text[max(0, start - 40):start]
            if not re.search(r"\.filter(Indexed)?\s*$", head):
                continue
            seen.add(start)
            sites.append((text[:start].count("\n") + 1, block))
    return sites


def check_payload_filters(root, provider_dir, texts):
    """[deep-audit-1045 A-03] Empty-message filters must keep media-only turns,
    through the one shared predicate."""
    violations = []
    total = 0
    for rel, text in texts.items():
        for line, block in payload_filter_sites(text):
            total += 1
            if PAYLOAD_PREDICATE_CALL in block:
                continue
            missing = [arm for arm in PAYLOAD_MEDIA_ARMS if arm not in block]
            violations.append(
                "%s:%d empty-message filter drops media-only turns (no %s, no shared "
                "%s) — a minis-model-use turn carrying only an image or an audio block "
                "is deleted and the bytes never reach the model"
                % (rel, line, " / ".join(missing) or "<media arm>", PAYLOAD_PREDICATE_CALL)
            )
    if total < MIN_EMPTY_FILTER_SITES:
        violations.append(
            "only %d empty-message filter site(s) found under the provider package "
            "(expected >= %d) — the filter was removed or renamed, which either loses the "
            "sanitizer defense or hides the media arm from this gate"
            % (total, MIN_EMPTY_FILTER_SITES)
        )
    predicate = texts.get(PAYLOAD_PREDICATE_FILE)
    if predicate is None:
        violations.append(
            "%s: the shared payload predicate file is gone — the three serializers are "
            "free to drift apart again" % PAYLOAD_PREDICATE_FILE
        )
        return violations, total
    if PAYLOAD_PREDICATE_DECL not in predicate:
        violations.append(
            "%s: '%s' declaration not found (the predicate was inlined or renamed)"
            % (PAYLOAD_PREDICATE_FILE, PAYLOAD_PREDICATE_DECL)
        )
        return violations, total
    alive = [arm for arm in PAYLOAD_MEDIA_ARMS + (PAYLOAD_TURN_IMAGE_ARM,) if arm not in predicate]
    if alive:
        violations.append(
            "%s: the payload predicate no longer weighs %s — that payload can only come "
            "from a media-only turn, so dropping the arm re-drops the user's turn"
            % (PAYLOAD_PREDICATE_FILE, ", ".join(alive))
        )
    return violations, total


def scan_root(root, exemptions=None):
    """Return (violations, scanned_files, per_file_report)."""
    exemptions = EXEMPTIONS if exemptions is None else exemptions
    provider_dir = os.path.join(root, PROVIDER_REL)
    violations = []
    report = []
    scanned = 0
    texts = {}
    if not os.path.isdir(provider_dir):
        return (["provider package not found at %s" % provider_dir], 0, [])

    for dirpath, _dirs, files in os.walk(provider_dir):
        for name in sorted(files):
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, provider_dir)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            texts[rel] = text
            when_hits = [m.start() for m in re.finditer(re.escape(WHEN_BRANCH), text)]
            has_filter = FILTER_CALL in text
            if not when_hits and not has_filter:
                continue
            scanned += 1

            exempt = exemptions.get(rel)
            if exempt is not None:
                symbol = exempt["symbol"]
                if symbol not in text:
                    violations.append(
                        "%s: stale exemption — cited symbol %r no longer exists; "
                        "re-check whether it really never serializes bytes" % (rel, symbol)
                    )
                    report.append((rel, "STALE-EXEMPT"))
                else:
                    report.append((rel, "exempt"))
                continue

            if IMAGE_FIELD not in text:
                line = text[: when_hits[0]].count("\n") + 1 if when_hits else 1
                violations.append(
                    "%s:%d handles tool-result parts but never references %s "
                    "(tool-result images would be dropped)" % (rel, line, IMAGE_FIELD)
                )
                report.append((rel, "missing-imageData"))
                continue

            bad_branch = False
            for start in when_hits:
                block = branch_block(text, start)
                line = text[:start].count("\n") + 1
                if block is None:
                    violations.append("%s:%d unbalanced ToolResult branch" % (rel, line))
                    bad_branch = True
                    continue
                if IMAGE_FIELD not in block:
                    violations.append(
                        "%s:%d ToolResult branch does not consume %s"
                        % (rel, line, IMAGE_FIELD)
                    )
                    bad_branch = True
            report.append((rel, "branch-without-imageData" if bad_branch else "ok"))

    # An exemption that no longer matches any scanned file is dead weight and
    # would silently excuse a future regression in that file.
    for rel, exempt in exemptions.items():
        if not any(r[0] == rel for r in report):
            violations.append(
                "%s: exemption is stale — file no longer handles tool-result parts "
                "(%s)" % (rel, exempt["reason"][:60])
            )

    payload_violations, _sites = check_payload_filters(root, provider_dir, texts)
    violations.extend(payload_violations)
    return (violations, scanned, report)


def selftest():
    """Fixtures proving the gate has teeth: a violating sample must be red, a
    compliant one green, and exemption rot must be caught."""
    tmp = tempfile.mkdtemp(prefix="trimg-gate-")
    try:
        pkg = os.path.join(tmp, PROVIDER_REL)
        os.makedirs(os.path.join(pkg, "anthropic"), exist_ok=True)

        def write(rel, body):
            path = os.path.join(pkg, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)

        # (a) violating: filter form, no imageData anywhere.
        write(
            "openai/ViolatingProvider.kt",
            "class V {\n"
            "    fun f(parts: List<Any>) {\n"
            "        for (tr in parts.filterIsInstance<AgentContentPart.ToolResult>()) {\n"
            "            put(\"content\", tr.content)\n"
            "        }\n"
            "    }\n"
            "}\n",
        )
        # (b) compliant: same shape, consumes the bytes.
        write(
            "gemini/CompliantProvider.kt",
            "class C {\n"
            "    fun f(parts: List<Any>) {\n"
            "        for (tr in parts.filterIsInstance<AgentContentPart.ToolResult>()) {\n"
            "            if (tr.imageData != null) put(\"blob\", tr.imageData)\n"
            "        }\n"
            "    }\n"
            "}\n",
        )
        # (c) when-branch form with the image handled in the branch body.
        write(
            "anthropic/WhenProvider.kt",
            "class W {\n"
            "    fun f(part: Any) = when (part) {\n"
            "        is AgentContentPart.ToolResult -> {\n"
            "            if (part.imageData != null) put(\"image\", part.imageData)\n"
            "        }\n"
            "        else -> Unit\n"
            "    }\n"
            "}\n",
        )
        # (d) file-level imageData present but the branch body itself ignores it.
        write(
            "anthropic/SplitProvider.kt",
            "class S {\n"
            "    fun other(p: Any) = p.imageData\n"
            "    fun f(part: Any) = when (part) {\n"
            "        is AgentContentPart.ToolResult -> {\n"
            "            put(\"content\", part.content)\n"
            "        }\n"
            "        else -> Unit\n"
            "    }\n"
            "}\n",
        )

        violations, scanned, report = scan_root(tmp)
        ok = True

        def check(label, condition, detail=""):
            nonlocal ok
            mark = "OK" if condition else "FAIL"
            print("  [%s] %s%s" % (mark, label, (" — " + detail) if detail and not condition else ""))
            if not condition:
                ok = False

        check("scanned 4 fixture files", scanned == 4, "scanned=%d" % scanned)
        check(
            "filter-form violation is red",
            any("ViolatingProvider.kt" in v for v in violations),
        )
        check(
            "compliant filter-form is green",
            not any("CompliantProvider.kt" in v for v in violations),
        )
        check("compliant when-branch is green", not any("WhenProvider.kt" in v for v in violations))
        check(
            "when-branch that ignores imageData inside its own body is red",
            any("SplitProvider.kt" in v and "ToolResult branch does not consume" in v for v in violations),
        )

        # Exemption with a live symbol is honoured; a dead one is rot.
        write(
            "played/ExemptProvider.kt",
            "fun sanitizeToolPairing() {}\n"
            "class E { fun f(p: Any) = when (p) {\n"
            "    is AgentContentPart.ToolResult -> put(\"x\")\n"
            "    else -> Unit\n"
            "} }\n",
        )
        live = {
            "played/ExemptProvider.kt": {
                "reason": "fixture",
                "symbol": "fun sanitizeToolPairing(",
            }
        }
        v_live, _, _ = scan_root(tmp, live)
        check("exemption with a live symbol is honoured", not any("ExemptProvider" in v for v in v_live))

        dead = {
            "played/ExemptProvider.kt": {
                "reason": "fixture",
                "symbol": "fun noLongerHere(",
            }
        }
        v_dead, _, _ = scan_root(tmp, dead)
        check(
            "exemption with a renamed symbol is reported as rot",
            any("stale exemption" in v for v in v_dead),
        )

        # [deep-audit-1045 A-03] media-only turns survive the empty-message filter
        def payload_tree():
            t = tempfile.mkdtemp(prefix="trimg-payload-")
            pkg = os.path.join(t, PROVIDER_REL)
            os.makedirs(pkg, exist_ok=True)

            def w(rel, body):
                path = os.path.join(pkg, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)

            w(PAYLOAD_PREDICATE_FILE,
              "internal fun LLMMessage.hasWirePayload(\n"
              "    isLastUserTurn: Boolean,\n"
              "    turnImageCount: Int,\n"
              "): Boolean =\n"
              "    contentParts.isNotEmpty() ||\n"
              "        content.isNotEmpty() ||\n"
              "        audioParts.isNotEmpty() ||\n"
              "        imageParts.isNotEmpty() ||\n"
              "        (isLastUserTurn && turnImageCount > 0)\n")
            for rel, var in (("openai/A.kt", "a"), ("openai/B.kt", "b"),
                             ("anthropic/C.kt", "c")):
                w(rel,
                  "class %s {\n"
                  "    fun f(raw: List<LLMMessage>, images: List<Any>) {\n"
                  "        val sites = raw\n"
                  "            .filterIndexed { index, m ->\n"
                  "                m.hasWirePayload(\n"
                  "                    isLastUserTurn = index == 0,\n"
                  "                    turnImageCount = images.size,\n"
                  "                )\n"
                  "            }\n"
                  "    }\n"
                  "}\n" % ("P" + var.upper()))
            return t, pkg

        p_root, p_pkg = payload_tree()
        try:
            v_clean, _sc, _rp = scan_root(p_root)
            check("media-aware filter sites are green",
                  not any("empty-message filter" in v or "payload predicate" in v
                          for v in v_clean),
                  str(v_clean[:2]))

            def mutate(rel, old, new_text):
                path = os.path.join(p_pkg, rel)
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
                assert old in body, "fixture mutation anchor missing in %s" % rel
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body.replace(old, new_text, 1))

            # (1) predicate loses the audio arm
            mutate(PAYLOAD_PREDICATE_FILE, "        audioParts.isNotEmpty() ||\n", "")
            v_audio, _, _ = scan_root(p_root)
            check("dropping the audio arm is red",
                  any(PAYLOAD_PREDICATE_FILE in v and "audioParts" in v for v in v_audio),
                  str(v_audio[:2]))
            mutate(PAYLOAD_PREDICATE_FILE, "        content.isNotEmpty() ||\n",
                   "        content.isNotEmpty() ||\n        audioParts.isNotEmpty() ||\n")

            # (2) predicate loses the request-level turn-image arm
            mutate(PAYLOAD_PREDICATE_FILE,
                   "        imageParts.isNotEmpty() ||\n"
                   "        (isLastUserTurn && turnImageCount > 0)\n",
                   "        imageParts.isNotEmpty()\n")
            v_turn, _, _ = scan_root(p_root)
            check("dropping the turn-image arm is red",
                  any(PAYLOAD_PREDICATE_FILE in v and "turnImageCount" in v for v in v_turn),
                  str(v_turn[:2]))
            mutate(PAYLOAD_PREDICATE_FILE,
                   "        imageParts.isNotEmpty()\n",
                   "        imageParts.isNotEmpty() ||\n"
                   "        (isLastUserTurn && turnImageCount > 0)\n")

            # (3) one site reverts to the legacy inline predicate
            mutate("openai/B.kt",
                   "                m.hasWirePayload(\n"
                   "                    isLastUserTurn = index == 0,\n"
                   "                    turnImageCount = images.size,\n"
                   "                )\n",
                   "                m.contentParts.isNotEmpty() || m.content.isNotEmpty()\n")
            v_site, _, _ = scan_root(p_root)
            check("a site reverted to the inline text-only predicate is red",
                  any("openai/B.kt" in v and "empty-message filter" in v for v in v_site),
                  str(v_site[:2]))
            mutate("openai/B.kt",
                   "                m.contentParts.isNotEmpty() || m.content.isNotEmpty()\n",
                   "                m.hasWirePayload(\n"
                   "                    isLastUserTurn = index == 0,\n"
                   "                    turnImageCount = images.size,\n"
                   "                )\n")

            # (4) the shared predicate file disappears
            os.remove(os.path.join(p_pkg, PAYLOAD_PREDICATE_FILE))
            v_gone, _, _ = scan_root(p_root)
            check("a missing payload predicate file is red",
                  any("payload predicate file is gone" in v for v in v_gone),
                  str(v_gone[:2]))
        finally:
            shutil.rmtree(p_root, ignore_errors=True)

        missing_dir, scanned_missing, _ = scan_root(os.path.join(tmp, "does-not-exist"))
        check(
            "missing provider package is a hard failure, not a green skip",
            bool(missing_dir) and scanned_missing == 0,
        )
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        print("tool_result_image_branch_check self-test:")
        return selftest()
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    violations, scanned, report = scan_root(root)
    for rel, status in report:
        print("  %-8s %s" % ("[" + status + "]", rel))
    if scanned == 0:
        violations.append(
            "no provider file handling tool-result parts was found — refusing to report green"
        )
    print("  scanned %d provider file(s)" % scanned)
    if violations:
        print("")
        print("❌ tool_result_image_branch_check findings:")
        for v in violations:
            print("   - %s" % v)
        return 1
    print("")
    print("✅ tool-result image guard: every provider serializer consumes ToolResult.imageData")
    return 0


if __name__ == "__main__":
    sys.exit(main())
