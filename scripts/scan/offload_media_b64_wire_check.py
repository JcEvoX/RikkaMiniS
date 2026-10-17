#!/usr/bin/env python3
"""
offload_media_b64_wire_check — deep-audit-1022 FIX-A gate.

Cross-process base64 wire contract (worker -> result.json -> main process):
  - Producer side (ModelExecutionService.kt, media_files encode sites) must use
    android.util.Base64.NO_WRAP, not Base64.DEFAULT (DEFAULT wraps at 76 chars
    + trailing newline).
  - Consumer side (ProviderExecutionGateway.kt) must use the MIME decoder
    (java.util.Base64.getMimeDecoder()), NOT the strict RFC 4648 decoder
    (java.util.Base64.getDecoder()), which throws IllegalArgumentException on
    any wrapped payload.

Usage: python3 offload_media_b64_wire_check.py <repo_root>
       python3 offload_media_b64_wire_check.py --selftest
Exit: 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import sys

GATEWAY_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "sandbox", "offload", "ProviderExecutionGateway.kt")
SERVICE_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "sandbox", "offload", "ModelExecutionService.kt")

# Code anchors (not comment anchors): these appear in executable lines only.
MEDIA_ENCODE_ANCHOR = 'put("data", android.util.Base64.encodeToString('
STRICT_DECODER = "Base64.getDecoder()"


def strip_comments(text):
    """Remove // line comments and /* */ block comments so anchor checks only
    see executable code (comment mentions must not satisfy or defeat a check)."""
    out = []
    for line in text.splitlines():
        code = line
        # strip // comments (naive but sufficient: no base64-string literals
        # in these files contain "//" that matters; anchors are put(...)/getDecoder)
        idx = code.find("//")
        if idx >= 0:
            code = code[:idx]
        out.append(code)
    return "\n".join(out)


def read(rel):
    with open(rel, "r", encoding="utf-8") as f:
        return f.read()


def check_tree(root):
    """Return (ok, [failure messages])."""
    failures = []
    gw_path = os.path.join(root, GATEWAY_REL)
    svc_path = os.path.join(root, SERVICE_REL)
    for p, label in ((gw_path, "ProviderExecutionGateway.kt"),
                     (svc_path, "ModelExecutionService.kt")):
        if not os.path.exists(p):
            failures.append("%s: file missing at %s" % (label, p))
            return False, failures

    # 1) Consumer side: gateway must not use the strict RFC 4648 decoder.
    gw_code = strip_comments(read(gw_path))
    if STRICT_DECODER in gw_code:
        failures.append("gateway uses strict %s (throws on wrapped payload); "
                        "use java.util.Base64.getMimeDecoder()" % STRICT_DECODER)
    if "java.util.Base64.getMimeDecoder()" not in gw_code:
        failures.append("gateway does not use java.util.Base64.getMimeDecoder() "
                        "(MIME decoder required for RFC 2045 wrapped input)")

    # 2) Producer side: the media_files encode sites must use NO_WRAP.
    svc_lines = strip_comments(read(svc_path)).splitlines()
    sites = [(i + 1, ln) for i, ln in enumerate(svc_lines)
             if MEDIA_ENCODE_ANCHOR in ln]
    if len(sites) == 0:
        failures.append("no media encode sites found (anchor %r missing) — "
                        "gate is stale, update the anchor" % MEDIA_ENCODE_ANCHOR)
    for lineno, ln in sites:
        if "Base64.DEFAULT" in ln:
            failures.append("ModelExecutionService.kt:%d uses Base64.DEFAULT on "
                            "the media wire (wraps at 76 chars + newline); use NO_WRAP" % lineno)
        elif "NO_WRAP" not in ln:
            failures.append("ModelExecutionService.kt:%d media encode uses neither "
                            "NO_WRAP nor DEFAULT — verify the flag" % lineno)

    return len(failures) == 0, failures


FIXTURE_COMPLIANT = {
    GATEWAY_REL: (
        "class G {\n"
        "    val data = runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()\n"
        "}\n"
    ),
    SERVICE_REL: (
        "class S {\n"
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "}\n"
    ),
}

FIXTURE_VIOLATION = {
    GATEWAY_REL: (
        "class G {\n"
        "    // comment mentioning java.util.Base64.getMimeDecoder() must not fool the gate\n"
        "    val data = runCatching { java.util.Base64.getDecoder().decode(b64) }.getOrNull()\n"
        "}\n"
    ),
    SERVICE_REL: (
        "class S {\n"
        '    // put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.DEFAULT))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "}\n"
    ),
}


def run_fixture(files, expect_ok, label):
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="b64wire-gate-")
    try:
        for rel, content in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        ok, failures = check_tree(tmp)
        if expect_ok and not ok:
            print("FAIL  %s: compliant fixture rejected: %s" % (label, failures))
            return False
        if not expect_ok and ok:
            print("FAIL  %s: violating fixture accepted" % label)
            return False
        print("PASS  %s" % label)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest():
    ok = True
    ok &= run_fixture(FIXTURE_COMPLIANT, True, "selftest: compliant fixture accepted")
    ok &= run_fixture(FIXTURE_VIOLATION, False, "selftest: violation fixture rejected")
    # Comment-anchored NO_WRAP in a commented-out line must NOT satisfy the producer check
    ok &= run_fixture({
        GATEWAY_REL: FIXTURE_COMPLIANT[GATEWAY_REL],
        SERVICE_REL: (
            "class S {\n"
            '    // put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
            "}\n"
        ),
    }, False, "selftest: commented-out NO_WRAP does not satisfy producer check")
    # Comment-anchored getMimeDecoder must NOT satisfy the consumer check
    ok &= run_fixture({
        GATEWAY_REL: (
            "class G {\n"
            "    // java.util.Base64.getMimeDecoder()\n"
            "    val data = runCatching { java.util.Base64.getDecoder().decode(b64) }.getOrNull()\n"
            "}\n"
        ),
        SERVICE_REL: FIXTURE_COMPLIANT[SERVICE_REL],
    }, False, "selftest: commented-out getMimeDecoder does not satisfy consumer check")
    if ok:
        print("OFFLOAD_MEDIA_B64_SELFTEST=PASS")
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(selftest())
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 < len(args):
            root = args[i + 1]
    elif args:
        root = args[0]
    if not root:
        print("usage: offload_media_b64_wire_check.py <repo_root> | --selftest", file=sys.stderr)
        sys.exit(2)
    ok, failures = check_tree(root)
    for f in failures:
        print("VIOLATION: %s" % f)
    if ok:
        print("OFFLOAD_MEDIA_B64_WIRE: OK (%d media encode sites NO_WRAP, consumer MIME decoder)" %
              len([l for l in strip_comments(read(os.path.join(root, SERVICE_REL))).splitlines()
                   if MEDIA_ENCODE_ANCHOR in l]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
