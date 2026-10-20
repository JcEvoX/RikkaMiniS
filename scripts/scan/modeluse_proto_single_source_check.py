#!/usr/bin/env python3
"""
Gate: FIX-A (deep-audit-1019 ISSUE-01) — the extra_headers / custom-endpoint
envelope dialect must live ONCE, in ModelUseProtocol.kt.

The bug: `parseExtraHeaders` and `parseCustomEndpointPath` still had a
hand-written copy in EACH process (`ModelExecutionService.kt` and
`ModelUseOffloadHandler.kt`) and the copies had drifted — the worker dropped a
present-but-not-an-object `extra_headers` with ZERO warning, accepted any
non-empty `endpoint` as a path (symbolic modes like `chat` were written into
`provider.absoluteEndpointOverride`), and never read `image_endpoint` at all.
The fix collapsed both into `ModelUseProtocol` with one-line delegates at the
call sites (the `parseChatExtraBody` precedent).

Checks assert:
  1. the dialect definitions live in ModelUseProtocol.kt, and every definition
     remaining in a caller file is a one-line DELEGATE into ModelUseProtocol
     (a re-collapsed implementation body fails);
  2. the caller files invoke through the `ModelUseProtocol.` prefix;
  3. the silent `optJSONObject("extra_headers")` branch does not come back in
     either caller file.

[dv14-A4] ISSUE-08 — same defect one layer over (the IMAGE passthrough
dialect: implicit top-level keys minus the reserved set, plus the explicit
`extra_body` / `extra_headers` / `endpoint_path` envelope). It was written
THREE times, each copy carrying its own 17-key `imageReservedKeys` set, and
two of the three copies were already unreachable — one dead, one feeding a
reader nobody called. A set that is never the one being read cannot be kept in
sync by discipline, so:

  4. neither caller may DEFINE the reserved set (`val … imageReservedKeys` /
     `val … IMAGE_PASSTHROUGH_RESERVED_KEYS`) — it lives in ModelUseProtocol;
  5. `parseImagePassthrough` must exist in ModelUseProtocol and the caller
     that owns the image path must actually go through it;
  6. the silent `optJSONObject("extra_body")` read must not come back in a
     caller — a present-but-not-an-object `extra_body` has to be reported.

Scope note: every check below is limited to ModelUseProtocol.kt and
CALLER_RELS; the gate never judges a file outside them.

Checks run on comment-stripped text, so prose in a KDoc cannot satisfy them.
Usage: python3 scripts/scan/modeluse_proto_single_source_check.py [repo_root]
       python3 scripts/scan/modeluse_proto_single_source_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

SRC = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"
PROTO_REL = SRC + "/ModelUseProtocol.kt"
CALLER_RELS = (
    SRC + "/ModelExecutionService.kt",
    SRC + "/ModelUseOffloadHandler.kt",
)
FUNCS = ("parseExtraHeaders", "parseCustomEndpointPath")
SILENT_BRANCH = 'optJSONObject("extra_headers")'
PREFIX = "ModelUseProtocol."
PROTO_TOKENS = (
    "fun parseExtraHeaders(",
    "fun parseCustomEndpointPath(",
    "extra_headers is not a JSON object",
    'safeOptString("image_endpoint"',
    'startsWith("/")',
    # [dv14-A4] ISSUE-08 tokens
    "fun parseImagePassthrough(",
    "internal val imageReservedKeys",
)

# [dv14-A4] ISSUE-08 — the image passthrough dialect's single source.
IMAGE_PARSER = "parseImagePassthrough"
IMAGE_PARSER_CALL = PREFIX + IMAGE_PARSER + "("
# The caller that owns the image path: `applyImagePassthrough` is called only
# from the worker's image route, and routing itself has one owner
# (`ModelUseProtocol.decideImageRoute`). The app-process handler owns no image
# call at all and must not grow one back.
IMAGE_CALLER_REL = SRC + "/ModelExecutionService.kt"
# A caller DEFINING the set is the defect; merely reading
# `ModelUseProtocol.imageReservedKeys` is fine, so the anchor requires `val`.
IMAGE_KEY_DEF_RE = re.compile(
    r"\bval\s+(?:imageReservedKeys|IMAGE_PASSTHROUGH_RESERVED_KEYS)\b",
)
SILENT_IMAGE_BRANCH = 'optJSONObject("extra_body")'


def code_only(text):
    """Text with comments removed — presence checks must not be satisfiable
    from prose. Block comments (KDoc) first, then line comments, keeping line
    structure so violation reports still point at real lines."""
    out_lines = []
    in_block = False
    for line in text.split("\n"):
        res = []
        i = 0
        n = len(line)
        while i < n:
            if in_block:
                end = line.find("*/", i)
                if end < 0:
                    i = n
                else:
                    in_block = False
                    i = end + 2
                continue
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if line.startswith("//", i):
                break
            res.append(line[i])
            i += 1
        out_lines.append("".join(res))
    return "\n".join(out_lines)


def lines_of(src, needle):
    return [i + 1 for i, line in enumerate(src.split("\n")) if needle in line]


def find_defs(src):
    """Lines that declare either dialect function in a caller file."""
    out = {}
    for f in FUNCS:
        pat = re.compile(r"(private |internal |public )?fun %s\s*\(" % f)
        out[f] = [i + 1 for i, line in enumerate(src.split("\n")) if pat.search(line)]
    return out


def is_delegate_def(src, decl_line):
    """A caller-file definition is legal only if the declaration and the next
    two lines form a one-line delegate into ModelUseProtocol (expression body
    or `{ return ModelUseProtocol.parse... }` up to a brace that closes within
    these lines)."""
    lines = src.split("\n")
    window = "\n".join(lines[decl_line - 1 : decl_line + 2])
    return PREFIX in window


def check(root):
    v = []
    proto_path = os.path.join(root, PROTO_REL)
    if not os.path.exists(proto_path):
        return ["ModelUseProtocol.kt: NOT FOUND at %s" % proto_path]
    proto = code_only(open(proto_path, encoding="utf-8").read())

    for token in PROTO_TOKENS:
        if token not in proto:
            v.append("ModelUseProtocol.kt: the single dialect is missing '%s'" % token)
    for f in FUNCS:
        for n in lines_of(proto, "fun %s(" % f):
            # definitions inside ModelUseProtocol are the single source; a
            # delegate to itself would be a collapse bug — flag it
            window = "\n".join(proto.split("\n")[n - 1 : n + 2])
            if "ModelUseProtocol.%s(" % f in window:
                v.append("ModelUseProtocol.kt:%d: the dialect delegates to itself" % n)

    for rel in CALLER_RELS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            v.append("%s: NOT FOUND at %s" % (os.path.basename(rel), path))
            continue
        name = os.path.basename(rel)
        src = code_only(open(path, encoding="utf-8").read())
        for f in FUNCS:
            if "ModelUseProtocol.%s(" % f not in src:
                v.append(
                    "%s: no prefixed call 'ModelUseProtocol.%s(' — the call "
                    "site must go through the single dialect" % (name, f)
                )
        defs = find_defs(src)
        for f, decls in defs.items():
            for n in decls:
                if not is_delegate_def(src, n):
                    v.append(
                        "%s:%d: 'fun %s(' is NOT a one-line delegate into "
                        "ModelUseProtocol — a second implementation body is "
                        "back" % (name, n, f)
                    )
        for n in lines_of(src, SILENT_BRANCH):
            v.append(
                "%s:%d: the silent branch '%s' is back — a "
                "present-but-not-an-object value would be dropped with zero "
                "warning" % (name, n, SILENT_BRANCH)
            )
        for n in lines_of(src, SILENT_IMAGE_BRANCH):
            v.append(
                "%s:%d: the silent branch '%s' is back — a "
                "present-but-not-an-object extra_body would be dropped with "
                "zero warning" % (name, n, SILENT_IMAGE_BRANCH)
            )
        for i, line in enumerate(src.split("\n")):
            if IMAGE_KEY_DEF_RE.search(line):
                v.append(
                    "%s:%d: the reserved-key set is DEFINED here again — it "
                    "lives in ModelUseProtocol.kt, next to the single "
                    "%s" % (name, i + 1, IMAGE_PARSER_CALL.rstrip("("))
                )
    # [dv14-A4] ISSUE-08: the image path has ONE parser, and its caller uses it.
    caller_path = os.path.join(root, IMAGE_CALLER_REL)
    if not os.path.exists(caller_path):
        v.append(
            "%s: NOT FOUND at %s"
            % (os.path.basename(IMAGE_CALLER_REL), caller_path)
        )
    else:
        caller_src = code_only(open(caller_path, encoding="utf-8").read())
        if IMAGE_PARSER_CALL not in caller_src:
            v.append(
                "%s: no '%s' — the image passthrough dialect must be parsed by "
                "the single implementation in ModelUseProtocol"
                % (os.path.basename(IMAGE_CALLER_REL), IMAGE_PARSER_CALL)
            )
    return v


GOOD_CALLER = """\
package p
class C {
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> =
        ModelUseProtocol.parseExtraHeaders(inputJson, warnings)

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
}
"""

# Fixture 7's caller: everything else right, the image delegation gone.
GOOD_CALLER_NO_IMAGE = GOOD_CALLER.replace(
    "    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =\n"
    "        ModelUseProtocol.parseImagePassthrough(inputJson).body\n",
    "",
)

# Fixture 6's caller: a second definition of the reserved-key set.
GOOD_CALLER_REDEFINES_KEYS = GOOD_CALLER.replace(
    "    private fun parseCustomEndpointPath(",
    '    private val imageReservedKeys: Set<String> = setOf("messages")\n'
    "\n"
    "    private fun parseCustomEndpointPath(",
)

BAD_CALLER_COPY = """\
package p
class C {
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> {
        val obj = JSONObject(inputJson)
        val eh = obj.optJSONObject("extra_headers") ?: return emptyMap()
        return mapOf("k" to (eh.optString("k")))
    }

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
}
"""

BAD_CALLER_NO_PREFIX = """\
package p
class C {
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> =
        parseExtraHeaders(inputJson, warnings)

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
}
"""

GOOD_PROTO = """\
package p
object ModelUseProtocol {
    internal val imageReservedKeys: Set<String> = setOf("messages", "model")

    fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> {
        val raw = obj.opt("extra_headers") ?: return emptyMap()
        warnings.add("extra_headers is not a JSON object (actual type: x)")
        return out
    }

    fun parseCustomEndpointPath(inputJson: String): String? {
        val raw = obj.safeOptString("image_endpoint", "")
        return if (raw.startsWith("/")) raw else null
    }

    fun parseImagePassthrough(inputJson: String): ImagePassthroughSpec {
        val body = LinkedHashMap<String, Any?>()
        return ImagePassthroughSpec(body = body)
    }
}
"""


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def selftest():
    import tempfile

    ok = True

    def run(root):
        violations = check(root)
        return violations

    # fixture 1: good tree → exit 0
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        for rel in CALLER_RELS:
            write(root, rel, GOOD_CALLER)
        bad = run(root)
        if bad:
            ok = False
            print("SELFTEST-FAIL good fixture must be clean:", *bad, sep="\n  ")

    # fixture 2: a second implementation body pasted back into a caller → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], BAD_CALLER_COPY)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("NOT a one-line delegate" in b or SILENT_BRANCH in b for b in bad):
            ok = False
            print("SELFTEST-FAIL re-collapsed copy must be caught:", bad)

    # fixture 3: delegate without the ModelUseProtocol. prefix → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], BAD_CALLER_NO_PREFIX)
        bad = run(root)
        if not any("no prefixed call" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL missing-prefix delegate must be caught:", bad)

    # fixture 4: silent optJSONObject branch back in a caller → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], BAD_CALLER_COPY)
        bad = run(root)
        if not any(SILENT_BRANCH in b for b in bad):
            ok = False
            print("SELFTEST-FAIL silent branch must be caught:", bad)

    # fixture 5: comments cannot satisfy the checks
    with tempfile.TemporaryDirectory() as root:
        commented = """\
package p
class C {
    // ModelUseProtocol.parseExtraHeaders( — prose only
    /* private fun parseCustomEndpointPath(inputJson: String): String? = */
    private fun other(x: Int): Int = x
}
"""
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], commented)
        bad = run(root)
        if not any("no prefixed call" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL comment-only references must not count:", bad)

    # [dv14-A4] ISSUE-08 fixture 6: a caller defines the reserved-key set
    # again (the shape both deleted copies had) → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER_REDEFINES_KEYS)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("reserved-key set is DEFINED here again" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a second reserved-key set must be caught:", bad)

    # [dv14-A4] ISSUE-08 fixture 7: the image-path caller stops delegating to
    # the single parser → must FAIL (an unused single source is the defect)
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER_NO_IMAGE)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("must be parsed by the single implementation" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a caller without the image delegation must be caught:", bad)

    if ok:
        print("MODELUSE_PROTO_SELFTEST=PASS")
        return 0
    return 1


def main():
    args = [a for a in sys.argv[1:] if a != "--selftest"]
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    if len(args) > 1:
        print(__doc__)
        sys.exit(2)
    root = args[0] if args else os.getcwd()
    violations = check(root)
    if violations:
        print("modeluse_proto_single_source_check: %d violation(s)" % len(violations))
        for v in violations:
            print("  " + v)
        sys.exit(1)
    print("modeluse_proto_single_source_check: clean (dialect single-sourced in ModelUseProtocol)")
    sys.exit(0)


if __name__ == "__main__":
    main()
