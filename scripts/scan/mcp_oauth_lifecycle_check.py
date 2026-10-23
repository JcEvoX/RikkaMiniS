#!/usr/bin/env python3
"""
Gate: FIX-D (deep-audit-1024 ISSUE-04) — one convergence point for a MCP
server's OAuth credentials and in-guest bridge file, run by every write path.

ISSUE-04 was three drifts of the same invariant:

  crack 1  `MCPIntegrationsScreen` switched an authorized server to STDIO (or
           cleared its OAuth block) and called only `setClientSecret(ctx, id,
           null)` — which removes ONLY `secret::<id>`. `tokens::<id>` and the
           bridge file `/var/minis/mcp-servers/oauth/<id>.json` (a LIVE bearer
           token, plaintext, readable by any guest shell) survived forever, and
           the STDIO form hides the Sign Out button, so no UI path could ever
           clear them.
  crack 2  editing an authorized server re-saved the endpoints without
           re-materializing the bridge, so the guest CLI refreshed against the
           old token_endpoint / client_id / client_secret / resource.
  crack 3  `MCPRepository.importJSON` overwrote a same-id entry last-write-wins
           and left the old OAuth state in place.

The fix converges all three in the DATA layer (`MCPRepository.add/update/
importJSON` → `MCPOAuthStore.reconcileForServer`), because the UI is only one of
the write paths (paste-import and backup-restore are others, and any future
entry point must inherit the rule).

Checks (all on comment/string-masked code, so a comment cannot satisfy them):

  S1  MCPOAuthStore.kt defines `reconcileForServer(` whose body calls BOTH
      `purge(` and `materializeBridgeFile(` — the two outcomes must stay inside
      one function, or "reconcile" degenerates into half a cleanup again.
  S2  the pure `decideReconcileAction(` body still maps something to PURGE
      (dropping the purge outcome is the silent way to re-open crack 1).
  S3  no function may remove only `secret::<id>` and count as a cleanup: every
      function whose body removes the secret key must also remove the tokens key
      and delete the bridge file. `setClientSecret` (a setter, not a cleanup) is
      the single explicit exemption — adding another one is a deliberate edit
      here, not a silent pass.
  S4  every function that removes the tokens key must also delete the bridge
      file (a token with no bridge-file delete = plaintext credentials left in
      the guest).
  R1  MCPRepository.add( body calls the convergence point.
  R2  MCPRepository.update( body calls the convergence point.
  R3  MCPRepository.importJSON( body calls the convergence point (or purge( —
      an imported OAuth-less entry has nothing to materialize).
  R4  when the repository routes through a private helper, that helper's body
      must call `MCPOAuthStore.reconcileForServer(` — the indirection has to
      land somewhere.
  R5  the repository must not use `setClientSecret(`: a secret-only verb is not
      a complete cleanup, and purging alone on every save would instead wipe a
      live token (both are the same mistake seen from two sides).

Deliberate tightening vs the ISSUE-04 write-up: `add`/`update` are NOT allowed
to satisfy R1/R2 with a bare `purge(` — purging on every save would destroy the
credentials of a correctly authorized server, i.e. crack 1's mirror image.

[da1029-E] Two UI-side criteria were added to the same gate, because they pin
the same invariant from the other end — "the sheet edits what is on disk":

  U1  the edit-sheet call site in `MCPIntegrationsScreen` must hand
      `MCPAddSheet` the LIVE entry (resolved from the listing with
      `servers.find { … }` and an elvis fallback) instead of the
      `editServer` snapshot captured at tap time. With the snapshot, a write
      that landed while the sheet was open (a reconcile from `add`/`update`,
      a paste-import, `reloadFromDisk`) was both displayed stale and then
      rolled back by Save — the reconcile this gate defends was undone by the
      UI. The elvis fallback is required so an entry deleted meanwhile does
      not silently turn the edit sheet into the add flow.

  [dv14-B3] U1 was found pinning the DEFECT as the only legal shape: the elvis
  keeps the edit sheet open when the entry was deleted externally (reloadFrom
  Disk / paste-import land while the sheet is open), and Save then stores
  setClientSecret under a deleted id — orphan credentials against the
  MCPRepository.kt invariant "credentials exist iff the saved config is a
  configured URL-transport OAuth server" — while update() returns false and is
  silently dropped. The elvis is now ALLOWED but no longer sufficient on its
  own: the call site must also CLOSE the sheet when the listing no longer
  contains the captured id, via a `LaunchedEffect` keyed on the listing whose
  body checks `servers.none { … }` and sets `editServer = null` (keyed on the
  listing so a change re-launches with the fresh value; state is mutated from
  the effect, never inline in composition). Both legal shapes pass:
  elvis + close effect (transitional), or no elvis + close effect.
  U2  every `var … by remember` in `MCPFormTab` whose initializer mentions
      `editServer` must be keyed on that entry, so a changed entry re-seeds
      the field states instead of leaving the old values on screen (1027-E
      ModelGroupDetailScreen / 1028-C EnvVarFormSheet are the same defect).

U2 is enumerated, not counted by hand: a hand-written "16 fields" list is
exactly what drifts. The check walks every `remember` in the function body and
reports the un-keyed ones by line. Keyed-on-the-entry is required rather than
any key: `remember(nonce)` would satisfy "has a key" while still never
re-seeding.

U3 ([dv14-B3]) — the form must not store credentials under a deleted id: every
function body in the UI file that calls `setClientSecret(` (both the Authorize
and the Save click handlers) must also check the listing still contains the id
(`servers.value.none { it.id == … }` guard), refusing instead of aiming at a
deleted id. A save refused at the guard writes no secret, calls no
`add`/`update`, and never reaches `onDone()`.

[da1034-A1] Two more criteria extend the same credential invariant to the
backup surface — the artifact archive is NOT gated by `includeSecrets`:

  A1  `ArtifactBackupScope` must keep a deny-set of credential directory names
      (literal `setOf("oauth")`) AND actually consult it while walking, at
      every level. The MCP OAuth bridge (`mcp-servers/oauth/<server>.json` —
      access_token / refresh_token / client_secret in plaintext, plus a
      pending `<server>.secret` handoff) lives under the mcp artifact root, so
      without this the daily auto-backup and every WebDAV copy of it shipped
      live bearer tokens. Anchored on the declaration + the `child.name in …`
      skip so a comment cannot satisfy it and a renaming cannot silently
      unhook it.
  A2  `ConfigBackup.import`'s artifact stage must run `stripMcpOAuthEntries(`
      BEFORE `ArtifactArchiver.extractZipByPrefix(`, the predicate
      (`isMcpOAuthEntryName`) must pin both the "mcp" and "oauth" segments,
      and the drop must be reported through `skipped`. A restore of an archive
      written by a pre-fix build (or crafted by hand) would otherwise
      overwrite this device's LIVE token bridge — fixing the exporter cannot
      protect archives that already exist.

[dv25-A1] The PARSE side of the same credential invariant (deep-audit-1045
A-01). The in-guest CLI writes camelCase oauth keys (`clientId`,
`authorizationEndpoint`, `tokenEndpoint`, `redirectURI` — main.py), this app
writes snake_case. Reading snake_case only left every field blank for a
CLI-written entry ⇒ `isConfigured` false ⇒ `MCPRepository.save()` — which runs
on the load of any entry without `createdAt`, and the CLI writes none — dropped
the `oauth` block outright, so the config evaporated from servers.json on the
next reload from disk or backup import (both go through the same `fromJson`).
The CLI's key set is DERIVED from main.py, so a new guest-side key turns this
gate red instead of silently evaporating.

  C1  every key the guest CLI writes is readable here: either spelled the same
      way, or mentioned by a key table (`listOf(<snake_case>, <camelCase>)`).
  C2  key tables are ordered primary-then-alias — snake_case first. A table
      whose only entry is the camelCase spelling (alias promoted to primary)
      makes the value this app wrote unreadable.
  C3  an aliased table must be consulted by `fromJson` — a fallback nobody
      reads is a fallback on paper.
  C4  `toJson` writes only snake_case keys: the disk/backup format must not
      fork just because the read side learned a second spelling.
  C5  `fromJson` keeps its "no endpoints = noise" null path (every property
      `isConfigured` requires is blank-checked).

All are fail-closed: sources that cannot be read are a violation.

Usage: python3 scripts/scan/mcp_oauth_lifecycle_check.py [repo_root]
       python3 scripts/scan/mcp_oauth_lifecycle_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

STORE_REL = "src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/MCPOAuthStore.kt"
REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/MCPRepository.kt"
UI_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/MCPIntegrationsScreen.kt"

SELFTEST_MARK = "MCP_OAUTH_LIFECYCLE_SELFTEST=PASS"

# [da1029-E] UI-side anchors.
UI_FORM_FUN = "MCPFormTab"
UI_LIST_HOLDER = "servers"
UI_ENTRY_PARAM = "editServer"
UI_KEY_TOKENS = (UI_ENTRY_PARAM, "liveServer")
UI_LIVE_PASS = "editServer = "
UI_RESOLVE_PARAM = "MCPAddSheet"

# [da1034-A1] artifact-backup surface.
SCOPE_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/ArtifactBackupScope.kt"
CONFIG_BACKUP_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/ConfigBackup.kt"
OAUTH_DENY_LITERAL = "oauth"
OAUTH_ENTRY_PREDICATE = "isMcpOAuthEntryName"
OAUTH_STRIP_HELPER = "stripMcpOAuthEntries"
OAUTH_EXTRACT_CALL = "ArtifactArchiver.extractZipByPrefix("
OAUTH_SKIP_REPORT = "oauth bridge entries"

# [dv20-a1 A-01] The artifact (output-asset) face of the same credential
# invariant: a credentials-free export must not archive servers.json verbatim.
ARTIFACT_ARCHIVER_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/ArtifactArchiver.kt"
ARTIFACT_WRITE_CALL = "ArtifactArchiver.writeEntries("
MCP_ARTIFACT_REDACTOR = "redactMcpArtifactEntry"
MCP_SECRET_STRIP = "stripExportedMcpSecrets"
DENIED_SET_RE = re.compile(
    r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*setOf\(\s*"' + OAUTH_DENY_LITERAL + r'"\s*\)'
)

# [dv25-A1] The PARSE side of the same credential invariant. The in-guest CLI
# (`minis-mcp-cli add --oauth-*`) writes camelCase keys while this app writes
# snake_case. Reading snake_case only made every field blank for a CLI-written
# entry ⇒ isConfigured false ⇒ MCPRepository.save() (it runs on the load of any
# entry without `createdAt`, and the CLI writes none) dropped the whole `oauth`
# object. Both entry points are the same parse: CLI add → reloadFromDisk, and
# backup import → importJSON.
#
# The CLI's key set is DERIVED from main.py rather than listed here, so a new
# key on the guest side turns this gate red instead of silently evaporating.
CONFIG_REL = "src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/MCPOAuthConfig.kt"
CLI_REL = "src/android/app/src/main/assets/default_mount/usr/local/lib/minis-mcp-cli/main.py"
KEY_TABLE_RE = re.compile(r"\bval\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*listOf\(([^)]*)\)")
KEY_LITERAL_RE = re.compile(r'"([^"]*)"')
CLI_OAUTH_DICT_RE = re.compile(r"\boauth\s*=\s*\{")
CLI_OAUTH_ITEM_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')
CLI_OAUTH_ITEM_SET_RE = re.compile(r'\boauth\[\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\]')
TOJSON_WRITE_RE = re.compile(r'\bput\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')
CONFIGURED_PROP_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.isNotBlank\(\)")

CONVERGE_DECL = "reconcileForServer("
DECIDE_DECL = "decideReconcileAction("
PURGE_OUTCOME = "PURGE"
PURGE_CALL = "purge("
MATERIALIZE_CALL = "materializeBridgeFile("
SECRET_REMOVE = "remove(secretKey("
TOKENS_REMOVE = "remove(tokensKey("
BRIDGE_DELETE = "deleteBridgeFile("
SECRET_ONLY_VERB = "setClientSecret("
REPO_FULL_CALL = "MCPOAuthStore.reconcileForServer("
REPO_HELPER_CALL = "reconcileOAuth("
REPO_HELPER_DECL = "reconcileOAuth"
WRITE_PATHS = ("add", "update", "importJSON")
IMPORT_ALSO_OK = ("MCPOAuthStore.purge(",)

# Functions allowed to touch ONE credential key on its own, with a reason.
HALF_CLEANUP_EXEMPT = {
    "setClientSecret": "setter: writes or clears the secret field only, never a cleanup entry point",
}

FUN_DECL_RE = re.compile(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def mask(src):
    """Length-preserving mask of comments and string-literal CONTENTS.

    Tokens are searched on the result, so a mention inside a comment or a log
    message can neither satisfy a check nor trip one, and offsets still map 1:1
    onto the original file for line reporting.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif c == '"':
            # Keep quotes, blank the contents (a log string is not a call site).
            if src.startswith('"""', i):
                j = src.find('"""', i + 3)
                end = n if j < 0 else j + 3
                for k in range(i + 3, end):
                    if out[k] != "\n":
                        out[k] = " "
                i = end
            else:
                i += 1
                while i < n and src[i] != '"':
                    if src[i] == "\\":
                        if out[i] != "\n":
                            out[i] = " "
                        i += 1
                        if i < n and out[i] != "\n":
                            out[i] = " "
                        i += 1
                        continue
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
                i += 1
        else:
            i += 1
    return "".join(out)


def mask_comments(src):
    """Like [mask] but KEEPS string-literal contents (comments only).

    The [da1034-A1] criteria are anchored on real string literals
    (`setOf("oauth")`, "mcp", "oauth"), which [mask] blanks out — so those two
    checks mask comments only, while structural checks that must ignore log
    text keep using [mask].
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        else:
            i += 1
    return "".join(out)

def line_of(code, index):
    return code.count("\n", 0, index) + 1


def body_at(code, sig_start):
    """Balanced `{...}` body for a declaration starting at [sig_start].

    Returns None when the declaration has an expression body (no brace before
    the next `fun`) — such a function holds no removals anyway.
    """
    lp = code.find("(", sig_start)
    if lp < 0:
        return None
    depth = 0
    rp = -1
    for j in range(lp, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                rp = j
                break
    if rp < 0:
        return None
    brace = code.find("{", rp)
    if brace < 0:
        return None
    # An expression-bodied declaration (`= ...`) would otherwise hand back the
    # NEXT function's body.
    if "fun " in code[rp:brace]:
        return None
    depth = 0
    j = brace
    while j < len(code):
        c = code[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[brace:j + 1]
        j += 1
    return None


def body_of(code, name):
    """Body of the first `fun <name>(` declaration in [code], or None."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", code)
    if not m:
        return None
    return body_at(code, m.start())


def functions(code):
    """[(name, body, line)] for every `fun <name>(` declaration."""
    out = []
    for m in FUN_DECL_RE.finditer(code):
        out.append((m.group(1), body_at(code, m.start()), line_of(code, m.start())))
    return out


PLAY = "the convergence point"


def check_store(code):
    v = []
    converge = body_of(code, "reconcileForServer")
    if converge is None:
        v.append(
            "MCPOAuthStore.kt: no `fun reconcileForServer(` with a body — the "
            "single convergence point for config changes is gone (ROT: update "
            "this gate if it was renamed)"
        )
    else:
        if PURGE_CALL not in converge:
            v.append(
                "MCPOAuthStore.kt: reconcileForServer does not call `%s` — a "
                "server that is no longer a configured URL-transport OAuth "
                "server must not keep tokens/secret/bridge" % PURGE_CALL
            )
        if MATERIALIZE_CALL not in converge:
            v.append(
                "MCPOAuthStore.kt: reconcileForServer does not call `%s` — an "
                "authorized server whose endpoints changed keeps a bridge file "
                "that points at the OLD token endpoint" % MATERIALIZE_CALL
            )
    decide = body_of(code, "decideReconcileAction")
    if decide is None:
        v.append(
            "MCPOAuthStore.kt: no `fun decideReconcileAction(` with a body — "
            "the tested decision function is gone"
        )
    elif PURGE_OUTCOME not in decide:
        v.append(
            "MCPOAuthStore.kt: decideReconcileAction never returns %s — nothing "
            "purges any more (the half-cleanup state is unreachable but so is "
            "the cleanup)" % PURGE_OUTCOME
        )

    for name, body, line in functions(code):
        if body is None:
            continue
        if name in HALF_CLEANUP_EXEMPT:
            continue
        if SECRET_REMOVE in body and (TOKENS_REMOVE not in body or BRIDGE_DELETE not in body):
            v.append(
                "MCPOAuthStore.kt:%d: `%s` removes the client secret without "
                "removing the tokens key and the bridge file — removing only "
                "`secret::<id>` is not a cleanup (ISSUE-04 crack 1). If this "
                "really is a plain setter, add it to HALF_CLEANUP_EXEMPT "
                "deliberately." % (line, name)
            )
        if TOKENS_REMOVE in body and BRIDGE_DELETE not in body:
            v.append(
                "MCPOAuthStore.kt:%d: `%s` forgets tokens but leaves the bridge "
                "file — the guest keeps a plaintext bearer token it can no "
                "longer refresh" % (line, name)
            )
    return v


def check_repo(code):
    v = []
    for path in WRITE_PATHS:
        body = body_of(code, path)
        if body is None:
            v.append(
                "MCPRepository.kt: no `fun %s(` with a body — the write path "
                "this gate pins is gone (ROT: update this gate)" % path
            )
            continue
        accepted = (REPO_HELPER_CALL, REPO_FULL_CALL)
        if path == "importJSON":
            accepted = accepted + IMPORT_ALSO_OK
        if not any(tok in body for tok in accepted):
            v.append(
                "MCPRepository.kt: `%s` does not reach %s (%s) — the saved "
                "config and the stored OAuth state drift apart again"
                % (path, PLAY, " or ".join(accepted))
            )
    if REPO_HELPER_DECL in code:
        helper = body_of(code, REPO_HELPER_DECL)
        if helper is None or REPO_FULL_CALL not in helper:
            v.append(
                "MCPRepository.kt: the `%s` indirection does not call `%s` — "
                "the write paths look wired but nothing reconciles"
                % (REPO_HELPER_DECL, REPO_FULL_CALL)
            )
    if SECRET_ONLY_VERB in code:
        v.append(
            "MCPRepository.kt:%d: uses `%s` as credential handling — that verb "
            "touches the client secret only; a repository path must go through "
            "%s" % (line_of(code, code.find(SECRET_ONLY_VERB)), SECRET_ONLY_VERB, REPO_FULL_CALL)
        )
    return v


def camel_to_snake(name):
    """`clientId` → `client_id`, `redirectURI` → `redirect_uri`.

    Used to DERIVE which spelling pairs with which: the alias the guest CLI
    writes and the snake_case primary this app writes are two spellings of the
    same field, so neither has to be listed in this gate.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.lower()

def brace_block(code, open_idx):
    """The `{...}` block starting at [open_idx] (index of the `{`), or None."""
    depth = 0
    j = open_idx
    while j < len(code):
        c = code[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[open_idx:j + 1]
        j += 1
    return None

def cli_oauth_keys(cli):
    """Keys the in-guest CLI writes into a server's `oauth` object, in order."""
    keys = []
    for m in CLI_OAUTH_DICT_RE.finditer(cli):
        block = brace_block(cli, m.end() - 1)
        if block is None:
            continue
        for item in CLI_OAUTH_ITEM_RE.finditer(block):
            if item.group(1) not in keys:
                keys.append(item.group(1))
    for m in CLI_OAUTH_ITEM_SET_RE.finditer(cli):
        if m.group(1) not in keys:
            keys.append(m.group(1))
    return keys

def check_config(code, cli):
    """[dv25-A1] The parse/write key contract of MCPOAuthConfig.

    C1  every key the guest CLI writes is readable here — either spelled the
        same way, or present as an alias whose snake_case primary is a key
        `toJson` writes (so the two ends describe the same field).
    C2  every key table is ordered primary-then-alias (snake_case first).
    C3  every aliased table is actually consumed by `fromJson` (a table the
        parser ignores is a fallback that exists only on paper).
    C4  `toJson` writes only snake_case keys — the on-disk / backup format must
        not fork because the read side learned a second spelling.
    C5  `fromJson` keeps its "no endpoints = noise" null path (every property
        `isConfigured` requires must be blank-checked before returning null).
    """
    v = []
    fromjson = body_of(code, "fromJson")
    tojson = body_of(code, "toJson")
    if fromjson is None:
        return ["MCPOAuthConfig.kt: no `fun fromJson(` with a body — the parse "
                "side of the oauth key contract is gone (ROT: update this gate)"]
    if tojson is None:
        return ["MCPOAuthConfig.kt: no `fun toJson(` with a body — the write "
                "side of the oauth key contract is gone (ROT: update this gate)"]

    written = []
    for m in TOJSON_WRITE_RE.finditer(tojson):
        if m.group(1) not in written:
            written.append(m.group(1))
    if not written:
        return ["MCPOAuthConfig.kt: `toJson` writes no keys — the write side of "
                "the oauth key contract cannot be checked (ROT: update this gate)"]

    cli_keys = cli_oauth_keys(cli)
    if not cli_keys:
        return ["cannot derive the in-guest CLI's oauth keys from %s — the alias "
                "contract has no source of truth (ROT: update this gate)" % CLI_REL]

    tables = []
    for m in KEY_TABLE_RE.finditer(code):
        tables.append((m.group(1), KEY_LITERAL_RE.findall(m.group(2))))

    # C2: direction. Runs before C1's coverage test so an alias promoted to
    # primary is reported as a direction defect (C2 owns it), not as an
    # unreadable key — one arm, one criterion.
    tabled = set()
    for name, keys in tables:
        tabled.update(keys)
        if len(keys) >= 2:
            if camel_to_snake(keys[1]) != keys[0]:
                v.append(
                    "MCPOAuthConfig.kt:%d: key table `%s` is not ordered "
                    "primary-then-alias ('%s' should be the snake_case primary of "
                    "'%s') — with the alias first, a CLI-written block wins over "
                    "the value this app saved"
                    % (line_of(code, code.find(name)), name, keys[0], keys[1])
                )
        elif keys and keys[0] != camel_to_snake(keys[0]) and camel_to_snake(keys[0]) in written:
            v.append(
                "MCPOAuthConfig.kt:%d: key table `%s` reads only '%s', the "
                "CLI's camelCase spelling, with no snake_case primary — the "
                "value this app wrote to disk would be ignored"
                % (line_of(code, code.find(name)), name, keys[0])
            )

    # C1: coverage. A CLI spelling with no home parses blank and takes the
    # whole oauth block down with it.
    for key in cli_keys:
        if key in written or key in tabled:
            continue
        v.append(
            "MCPOAuthConfig.kt: `fromJson` does not read '%s', the key the "
            "in-guest CLI writes for this field (%s) — the whole oauth block "
            "parses blank, isConfigured turns false and MCPRepository.save() "
            "drops it on the next reload" % (key, CLI_REL)
        )

    # C3: wiring. Derive the table names from the file, not from a list here.
    for name, keys in tables:
        if len(keys) < 2 or camel_to_snake(keys[1]) != keys[0]:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", fromjson) is None:
            v.append(
                "MCPOAuthConfig.kt:%d: key table `%s` is declared but `fromJson` "
                "never consults it — the alias exists only on paper"
                % (line_of(code, code.find(name)), name)
            )

    # C4: the write side stays snake_case (old configs and backups unchanged).
    for key in written:
        if key != key.lower():
            v.append(
                "MCPOAuthConfig.kt:%d: `toJson` writes the camelCase key '%s' — "
                "the disk/backup format must stay snake_case; only the read side "
                "accepts the CLI's spelling" % (line_of(code, tojson.find('"%s"' % key)), key)
            )

    # C5: the null path. Derive the required properties from `isConfigured`'s
    # own getter — scanning the whole file would also catch `it.isNotBlank()`
    # in `toJson`'s takeIf and invent a property named `it`.
    cfg_at = code.find("isConfigured")
    required = []
    if cfg_at >= 0:
        stop = code.find("\n\n", cfg_at)
        scope = code[cfg_at:min(stop, cfg_at + 400) if stop > 0 else cfg_at + 400]
        for m in CONFIGURED_PROP_RE.finditer(scope):
            if m.group(1) not in required:
                required.append(m.group(1))
    if not required:
        v.append(
            "MCPOAuthConfig.kt: cannot derive the required oauth properties from "
            "`isConfigured` — the null path below has nothing to be checked "
            "against (ROT: update this gate)"
        )
        return v
    for prop in required:
        if (prop + ".isBlank()") not in fromjson:
            v.append(
                "MCPOAuthConfig.kt: `fromJson` no longer treats a blank `%s` as "
                "unconfigured — an oauth block with no endpoints would be parsed "
                "as configuration" % prop
            )
    if required and "null" not in fromjson:
        v.append(
            "MCPOAuthConfig.kt: `fromJson` has no null path — an absent/empty "
            "oauth block must still parse as null"
        )
    return v

def balanced(code, start, open_ch, close_ch):
    """Index of the char closing the group opened at [start], or -1."""
    depth = 0
    j = start
    while j < len(code):
        if code[j] == open_ch:
            depth += 1
        elif code[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def remember_sites(body):
    """[(offset, keys, expr)] for every `remember(...)`/`remember {...}`.

    The identifier must be followed by `(` or `{` — that keeps
    `rememberCoroutineScope(` / `rememberModalBottomSheetState(` out, and the
    calling code is passed already comment/string-masked.
    """
    out = []
    for m in re.finditer(r"\bremember\s*([({])", body):
        i = m.start()
        closer = m.group(1)
        if closer == "{":
            keys = ""
            end = balanced(body, m.end() - 1, "{", "}")
        else:
            kend = balanced(body, m.end() - 1, "(", ")")
            if kend < 0:
                continue
            keys = body[m.end():kend]
            brace = body.find("{", kend)
            end = balanced(body, brace, "{", "}") if brace >= 0 else kend
        if end < 0:
            end = len(body) - 1
        out.append((i, keys, body[i:end + 1]))
    return out


def check_ui(code):
    """[da1029-E] U1 + U2 — the edit sheet must edit the live entry.

    Returns (violations, keyed_field_count) so the real-repo run can report how
    many field states the enumerated check actually walked (a check that lost
    its subject must not read as green).
    """
    v = []
    m = re.search(r"\bfun\s+" + re.escape(UI_FORM_FUN) + r"\s*\(", code)
    if not m:
        v.append(
            "MCPIntegrationsScreen.kt: no `fun %s(` — the form whose "
            "field-state keys this check pins is gone (ROT: update this gate)"
            % UI_FORM_FUN
        )
        return v, 0
    body = body_at(code, m.start())
    body_off = code.find(body, m.start()) if body else -1
    if body is None or body_off < 0:
        v.append(
            "MCPIntegrationsScreen.kt: `%s` has no brace body — this check "
            "cannot see its field states (ROT: update this gate)" % UI_FORM_FUN
        )
        return v, 0
    enforced = 0
    for off, keys, expr in remember_sites(body):
        if UI_ENTRY_PARAM not in expr:
            continue
        line = line_of(code, body_off + off)
        if not any(tok in keys for tok in UI_KEY_TOKENS):
            v.append(
                "MCPIntegrationsScreen.kt:%d: a `remember` seeded from `%s` has "
                "no key on that entry — an entry changed behind the sheet keeps "
                "displaying (and Save writes back) the values it was opened with"
                % (line, UI_ENTRY_PARAM)
            )
        else:
            enforced += 1
    if enforced == 0:
        v.append(
            "MCPIntegrationsScreen.kt: not one keyed `remember` seeded from "
            "`%s` was found in `%s` — the field states moved or the enumerated "
            "check lost its subject (ROT: re-point it)" % (UI_ENTRY_PARAM, UI_FORM_FUN)
        )

    resolve = re.search(
        r"\bval\s+([A-Za-z_]\w*)\s*=\s*" + re.escape(UI_LIST_HOLDER) + r"\.find\s*\{([^}]*)\}",
        code,
    )
    if not resolve:
        v.append(
            "MCPIntegrationsScreen.kt: no `val <live> = %s.find { … }` — the "
            "sheet is handed the tap-time `%s` snapshot, so a write that landed "
            "while the sheet was open gets rolled back by Save"
            % (UI_LIST_HOLDER, UI_ENTRY_PARAM)
        )
        return v, enforced
    live_name = resolve.group(1)
    tail = code[resolve.end():resolve.end() + 40].lstrip()
    # [dv14-B3] close-on-disappear. The elvis alone used to be REQUIRED here —
    # that pinned the defect as the only legal shape (see docstring). Now the
    # elvis is allowed, but the call site must also CLOSE the sheet when the
    # listing no longer contains the captured id: a LaunchedEffect keyed on the
    # listing whose body checks `servers.none { … }` and sets `editServer =
    # null`.
    close = re.search(
        r"LaunchedEffect\([^)]*" + re.escape(UI_LIST_HOLDER) + r"[^)]*\)\s*\{"
        r"[^{}]*" + re.escape(UI_LIST_HOLDER) + r"\.none\s*\{[^{}]*\}"
        r"[^{}]*" + re.escape(UI_ENTRY_PARAM) + r"\s*=\s*null",
        code,
    )
    if close is None:
        if tail.startswith("?:"):
            v.append(
                "MCPIntegrationsScreen.kt: the elvis fallback keeps the edit "
                "sheet open after the entry was deleted externally, and no "
                "close-on-disappear effect was found — Save then stores "
                "setClientSecret under a deleted id (orphan credentials) while "
                "update() returns false and is silently dropped. Require a "
                "LaunchedEffect keyed on the listing whose body checks "
                "`servers.none { … }` and sets `editServer = null`"
            )
        else:
            v.append(
                "MCPIntegrationsScreen.kt: the live-entry lookup has no `?: ` "
                "fallback and no close-on-disappear effect — an entry deleted "
                "while the sheet is open would pass null to %s, silently "
                "turning the edit sheet into the add flow"
                % UI_RESOLVE_PARAM
            )
    if "%s%s," % (UI_LIVE_PASS, live_name) not in code:
        v.append(
            "MCPIntegrationsScreen.kt: `%s` is resolved but never passed as "
            "`%s%s` — the sheet still receives the snapshot"
            % (live_name, UI_LIVE_PASS, live_name)
        )

    # [dv14-B3] U3 — the form must not store credentials under a deleted id.
    # Per call site: every `setClientSecret(` in the form body (Authorize, Save)
    # must have a presence guard between its own click handler's start and the
    # call (`servers.value.none { … }`), refusing instead of aiming at a
    # deleted id. A body-level check is not enough — one handler's guard would
    # satisfy the other's removal (m2 caught that). Fail-closed the other way:
    # no call sites = nothing to orphan.
    guard_pat = re.escape(UI_LIST_HOLDER) + r"\.value\.none \{"
    for occ in re.finditer(re.escape(SECRET_ONLY_VERB), body):
        span_start = body.rfind("onClick", 0, occ.start())
        if span_start < 0:
            span_start = 0
        if not re.search(guard_pat, body[span_start:occ.start()]):
            v.append(
                "MCPIntegrationsScreen.kt:%d: the form stores credentials via "
                "`%s` but the click handler never checks the listing still "
                "contains the id (no `%s` before the call) — an entry deleted "
                "while the sheet was open gets orphan credentials, and "
                "update() returns false and is silently dropped"
                % (
                    line_of(code, body_off + occ.start()),
                    SECRET_ONLY_VERB,
                    UI_LIST_HOLDER + ".value.none {",
                )
            )
            break
    return v, enforced


def check_backup_scope(code):
    """[da1034-A1] A1 — the artifact walk must drop the MCP oauth bridge dir.

    [code] is comment-masked: the deny-set literal and the skip must both be
    real code, so a comment cannot satisfy them.
    """
    v = []
    m = DENIED_SET_RE.search(code)
    if not m:
        v.append(
            "ArtifactBackupScope.kt: no `<val> = setOf(\"" + OAUTH_DENY_LITERAL
            + "\")` deny-set — the MCP OAuth token bridge "
            "(mcp-servers/oauth/<server>.json holds LIVE access_token / refresh_token "
            "/ client_secret in plaintext) is selected into the artifact archive "
            "again, and from there into every auto-backup and WebDAV copy"
        )
        return v
    name = m.group(1)
    guard = "child.name in %s" % name
    at = code.find(guard)
    if at < 0:
        v.append(
            "ArtifactBackupScope.kt: %s is declared but the walk never skips on it "
            "(no `%s`) — the deny-set is inert" % (name, guard)
        )
    elif "continue" not in code[at:at + 120]:
        v.append(
            "ArtifactBackupScope.kt: `%s` (line %d) is not a `continue` skip — the "
            "walk still descends into the credential directory"
            % (guard, line_of(code, at))
        )
    return v

def check_backup_import(scoped, masked):
    """[da1034-A1] A2 — Stage 9 must drop mcp/oauth entries before extraction.

    [scoped] keeps string literals (predicate body + report message); [masked]
    blanks them and is used for the structural call-site ordering.
    """
    v = []
    decl = re.search(r"\bfun\s+" + re.escape(OAUTH_ENTRY_PREDICATE) + r"\s*\(", scoped)
    if not decl:
        v.append(
            "ConfigBackup.kt: no `fun " + OAUTH_ENTRY_PREDICATE + "(` — a restore can "
            "no longer tell a bridge entry from a data file, and a pre-fix archive "
            "rewrites this device's live token bridge verbatim"
        )
    else:
        body = body_at(scoped, decl.start())
        if body is None or ('"mcp"') not in body or ('"' + OAUTH_DENY_LITERAL + '"') not in body:
            v.append(
                "ConfigBackup.kt: `" + OAUTH_ENTRY_PREDICATE + "` body must pin BOTH "
                "the \"mcp\" and \"" + OAUTH_DENY_LITERAL + "\" segments — a "
                "predicate that matches nothing (or everything) is not a guard"
            )
    imp = body_of(masked, "import")
    if imp is None:
        v.append("ConfigBackup.kt: no `fun import(` body to inspect")
    else:
        f = imp.find(OAUTH_STRIP_HELPER + "(")
        x = imp.find(OAUTH_EXTRACT_CALL)
        if f < 0:
            v.append(
                "ConfigBackup.kt: import() never calls `" + OAUTH_STRIP_HELPER + "(` — "
                "the artifact archive is extracted as-is"
            )
        elif x < 0:
            v.append(
                "ConfigBackup.kt: import() no longer calls `" + OAUTH_EXTRACT_CALL
                + "` — the artifact stage moved; re-anchor this gate"
            )
        elif f > x:
            v.append(
                "ConfigBackup.kt: `" + OAUTH_STRIP_HELPER + "(` runs AFTER `"
                + OAUTH_EXTRACT_CALL + "` (lines %d/%d relative to import) — extraction "
                "already wrote the live token bridge; the filter must come first"
                % (imp[:f].count("\n") + 1, imp[:x].count("\n") + 1)
            )
        # [da1034-A1] Wiring the filter is not enough — extraction must CONSUME
        # the filtered archive. Binding `filtered` and then extracting the raw
        # temp zip keeps every bridge entry in the stream; the two criteria
        # above are blind to the operand (caught by mutation m2 in
        # probe-a1/mut/mutations.log).
        if f >= 0 and x >= 0:
            m_strip = re.search(
                re.escape(OAUTH_STRIP_HELPER)
                + r"\s*\(\s*(?:[A-Za-z_]\w*\s*=\s*)?[\w.]+\s*,\s*"
                + r"(?:[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\s*\)",
                imp,
            )
            if m_strip is None:
                v.append(
                    "ConfigBackup.kt: `" + OAUTH_STRIP_HELPER + "(` call shape not understood "
                    "— cannot tell which archive reaches `" + OAUTH_EXTRACT_CALL + "`; bind "
                    "the output as `" + OAUTH_STRIP_HELPER + "(src, dst)` (fail-closed, "
                    "re-anchor this gate if the call was intentionally reshaped)"
                )
            else:
                dst = m_strip.group(1)
                if not re.search(
                    re.escape(OAUTH_EXTRACT_CALL) + r"\s*" + re.escape(dst) + r"\b",
                    imp[m_strip.end():],
                ):
                    v.append(
                        "ConfigBackup.kt: import() extracts something other than the filtered "
                        "archive — `" + OAUTH_EXTRACT_CALL + dst + ", …)` must be the "
                        "oauth-stripped zip; the raw temp zip still carries the mcp/oauth "
                        "bridge entries"
                    )
    imp_scoped = body_of(scoped, "import")
    if imp_scoped is not None and OAUTH_SKIP_REPORT not in imp_scoped:
        v.append(
            "ConfigBackup.kt: import() drops bridge entries without reporting it — "
            "\"" + OAUTH_SKIP_REPORT + "\" must reach `skipped`"
        )
    return v

def argument_text(code, call_start):
    """Parenthesised argument list of the call starting at [call_start]."""
    lp = code.find("(", call_start)
    if lp < 0:
        return ""
    depth = 0
    for j in range(lp, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return code[lp:j + 1]
    return code[lp:]


def check_artifact_redaction(scoped, masked, archiver_masked):
    """[dv20-a1 A-01] The artifact face of a credentials-free backup.

    Returns three violation groups, one per decision:
      r1 the pack loop reaches the redactor `redactMcpArtifactEntry`, gated on
         `!includeSecrets` so an explicit with-credentials export is untouched;
      r2 the redactor DELEGATES to Stage 5's `stripExportedMcpSecrets` (one rule
         for one credential shape) and returns null when the bytes cannot be
         parsed instead of writing them through;
      r3 `writeEntries` drops — never writes — an entry whose transform
         returned null, and reports the drop.

    Anchored on the decision (who redacts, which bytes are dropped), not on a
    call's spelling: the redactor may be passed inline or bound to a local
    first. [scoped] keeps string literals; [masked]/[archiver_masked] blank
    them for the structural work.
    """
    r1, r2, r3 = [], [], []

    # -- R1: the pack face reaches the redactor, gated on credentials-free.
    m_root = re.search(r"val\s+isMcpRoot\s*=", masked)
    if not m_root:
        r1.append(
            "ConfigBackup.kt: no `val isMcpRoot =` in the artifact pack loop — the "
            "mcp artifact root is no longer distinguished; re-anchor this gate"
        )
        return r1, r2, r3
    call = masked.find(ARTIFACT_WRITE_CALL, m_root.end())
    if call < 0:
        r1.append(
            "ConfigBackup.kt: the artifact pack loop never calls `"
            + ARTIFACT_WRITE_CALL + "` — re-anchor this gate"
        )
        return r1, r2, r3
    args = argument_text(masked, call)
    window = masked[m_root.end():call]
    reachable = MCP_ARTIFACT_REDACTOR in args
    if not reachable:
        for ident in set(re.findall(r"\b([A-Za-z_]\w*)\b", args)):
            if re.search(
                r"val\s+" + re.escape(ident) + r"\s*=[\s\S]{0,300}?"
                + re.escape(MCP_ARTIFACT_REDACTOR),
                window,
            ):
                reachable = True
                break
    if not reachable:
        r1.append(
            "ConfigBackup.kt: `" + ARTIFACT_WRITE_CALL + "` is handed no `"
            + MCP_ARTIFACT_REDACTOR + "(` — a credentials-free export archives "
            "mcp-servers/servers.json verbatim, so headers/env/--token= args/url "
            "query keys ride into the zip (and into every WebDAV copy of it)"
        )
    if not re.search(r"!\s*includeSecrets", window):
        r1.append(
            "ConfigBackup.kt: the artifact redactor is not gated on `!includeSecrets` "
            "— an explicit with-credentials export (includeSecrets=true) would be "
            "silently redacted too"
        )

    # -- R2: one rule, and it must fail closed.
    body = body_of(masked, MCP_ARTIFACT_REDACTOR)
    body_scoped = body_of(scoped, MCP_ARTIFACT_REDACTOR)
    if body is None:
        r2.append(
            "ConfigBackup.kt: no `fun " + MCP_ARTIFACT_REDACTOR + "(` — the pack face "
            "has no redactor to hand the archiver; re-anchor this gate"
        )
    else:
        if MCP_SECRET_STRIP + "(" not in body:
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` does not call `"
                + MCP_SECRET_STRIP + "(` — a second, divergent secret rule now sits "
                "next to Stage 5's, which is how the two faces drift apart"
            )
        if body_scoped is not None and ('"headers"' in body_scoped or '"env"' in body_scoped):
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` blanks secret values "
                "itself (it names `headers`/`env`) instead of delegating to "
                + MCP_SECRET_STRIP + " — the two faces will drift"
            )
        if not re.search(r"catch\s*\([^)]*\)\s*\{[^}]*\bnull\b", body):
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` has no parse-failure "
                "path returning null — unredactable bytes are written into a "
                "credentials-free archive verbatim; it must fail closed"
            )

    # -- R3: the archiver drops a rejected entry and says so.
    wb = body_of(archiver_masked, "writeEntries")
    if wb is None:
        r3.append(
            "ArtifactArchiver.kt: no `fun writeEntries(` — the archive face moved; "
            "re-anchor this gate"
        )
        return r1, r2, r3
    if "transform(" not in wb:
        r3.append(
            "ArtifactArchiver.kt: `writeEntries` never applies its transform — a caller "
            "that hands one is ignored and every entry is archived verbatim"
        )
        return r1, r2, r3
    m_res = re.search(r"val\s+(\w+)\s*=\s*transform\(\s*[^)]*\)", wb)
    if not m_res:
        r3.append(
            "ArtifactArchiver.kt: the transform result is not bound to a local "
            "(`val x = transform(...)`) — nothing can tell a rejected entry from an "
            "accepted one; bind it (fail-closed, re-anchor if intentionally reshaped)"
        )
        return r1, r2, r3
    ident = m_res.group(1)
    if not re.search(r"\b" + re.escape(ident) + r"\s*==\s*null", wb):
        r3.append(
            "ArtifactArchiver.kt: the transform result is never compared to null — a "
            "rejected entry is not dropped"
        )
        return r1, r2, r3
    null_pos = wb.find(ident + " == null")
    next_continue = wb.find("continue", null_pos)
    next_write = wb.find("putNextEntry", null_pos)
    if next_continue < 0 or (0 <= next_write < next_continue):
        r3.append(
            "ArtifactArchiver.kt: a null transform result does not `continue` before "
            "`putNextEntry` — the rejected entry is archived anyway; it must fail closed"
        )
    if "droppedSink" not in wb:
        r3.append(
            "ArtifactArchiver.kt: a dropped entry is not reported to `droppedSink` — the "
            "caller's file/skip accounting silently overcounts the archive"
        )
    return r1, r2, r3


def read(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def run(root):
    if not os.path.isdir(root):
        print("mcp_oauth_lifecycle_check: not a directory: %s" % root)
        return 2
    try:
        store = mask(read(root, STORE_REL))
        repo = mask(read(root, REPO_REL))
    except OSError as exc:
        print("mcp_oauth_lifecycle_check: cannot read sources: %s" % exc)
        return 2
    violations = check_store(store) + check_repo(repo)
    # [dv25-A1] parse/write key contract (fail-closed: unreadable = violation).
    # Comments are masked, string literals are NOT — the criteria are anchored
    # on the key spellings themselves.
    try:
        config_raw = read(root, CONFIG_REL)
        cli_raw = read(root, CLI_REL)
    except OSError as exc:
        violations.append(
            "[dv25-A1] cannot read the oauth key-contract sources (%s) — a "
            "CLI-written oauth block can evaporate again" % exc
        )
    else:
        violations += check_config(mask_comments(config_raw), cli_raw)
    # [da1034-A1] artifact-backup surface (fail-closed: unreadable = violation).
    try:
        scope_raw = read(root, SCOPE_REL)
        backup_raw = read(root, CONFIG_BACKUP_REL)
        archiver_raw = read(root, ARTIFACT_ARCHIVER_REL)
    except OSError as exc:
        violations.append(
            "[da1034-A1] cannot read the artifact-backup sources (%s) — the oauth "
            "token bridge is no longer guarded on the backup path" % exc
        )
    else:
        violations += check_backup_scope(mask_comments(scope_raw))
        violations += check_backup_import(mask_comments(backup_raw), mask(backup_raw))
        v_pack, v_rule, v_arch = check_artifact_redaction(
            mask_comments(backup_raw), mask(backup_raw), mask(archiver_raw))
        violations += v_pack + v_rule + v_arch
    ui_path = os.path.join(root, UI_REL)
    ui_keyed = None
    if os.path.exists(ui_path):
        ui_v, ui_keyed = check_ui(mask(read(root, UI_REL)))
        violations += ui_v
    else:
        # Drift-detector convention: gates in this suite may be run against a
        # mini tree holding only the files a fixture is about. The UI criteria
        # only apply where the screen exists; the CD check on the real repo
        # fails loudly if the file is deleted.
        print("note: %s absent — UI criteria (U1/U2) skipped" % UI_REL)
    if violations:
        print("FAIL mcp_oauth_lifecycle_check")
        for item in violations:
            print("  " + item)
        return 1
    note = "" if ui_keyed is None else "; %d form field state(s) keyed on the entry" % ui_keyed
    print(
        "PASS mcp_oauth_lifecycle_check (one reconcile point; add/update/import "
        "all wired; no secret-only cleanup path; edit sheet edits the live entry; "
        "oauth bridge never archived and never restored; mcp servers.json redacted "
        "on a credentials-free export; every CLI-written oauth key readable by "
        "fromJson with snake_case primary first and the disk format unchanged%s)"
        % note
    )
    return 0


# --------------------------------------------------------------------------
# selftest: every criterion gets a fixture that trips exactly it
# --------------------------------------------------------------------------

GOOD_STORE = """
object MCPOAuthStore {
    private fun secretKey(server: String) = "secret::" + server
    private fun tokensKey(server: String) = "tokens::" + server

    fun setClientSecret(context: Context, server: String, secret: String?) {
        val p = prefs(context)
        if (secret.isNullOrEmpty()) {
            p.edit().remove(secretKey(server)).apply()
        } else {
            p.edit().putString(secretKey(server), secret).apply()
        }
    }

    fun signOut(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).apply()
        deleteBridgeFile(context, server)
    }

    fun purge(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).remove(secretKey(server)).apply()
        deleteBridgeFile(context, server)
    }

    internal enum class ReconcileAction { PURGE, KEEP, REFRESH_BRIDGE }

    internal fun decideReconcileAction(
        urlTransport: Boolean,
        oauth: MCPOAuthConfig?,
        hasTokens: Boolean,
    ): ReconcileAction = when {
        !usableOAuth(urlTransport, oauth) -> ReconcileAction.PURGE
        hasTokens -> ReconcileAction.REFRESH_BRIDGE
        else -> ReconcileAction.KEEP
    }

    fun reconcileForServer(context: Context, server: String, urlTransport: Boolean, oauth: MCPOAuthConfig?, serverUrl: String?) {
        val stored = tokens(context, server)
        when (decideReconcileAction(urlTransport, oauth, stored != null)) {
            ReconcileAction.PURGE -> purge(context, server)
            ReconcileAction.KEEP -> Unit
            ReconcileAction.REFRESH_BRIDGE -> {
                val cfg = requireNotNull(oauth)
                materializeBridgeFile(context, server, BridgePayload(accessToken = stored!!.accessToken, tokenEndpoint = cfg.tokenEndpoint))
            }
        }
    }

    fun materializeBridgeFile(context: Context, server: String, payload: BridgePayload) {}

    fun deleteBridgeFile(context: Context, server: String) {
        runCatching { bridgeFile(context, server).delete() }
    }
}
"""

GOOD_REPO = """
class MCPRepository(private val context: Context) {
    fun add(server: MCPServerConfig): Boolean {
        if (server.id.isBlank()) return false
        _servers.value = _servers.value.filter { it.id != server.id } + server
        save()
        reconcileOAuth(server)
        return true
    }

    fun update(server: MCPServerConfig): Boolean {
        if (_servers.value.none { it.id == server.id }) return false
        _servers.value = _servers.value.map { if (it.id == server.id) server else it }
        save()
        reconcileOAuth(server)
        return true
    }

    fun importJSON(text: String): List<MCPServerConfig> {
        val parsed = parseImportRoot(JSONObject(text))
        if (parsed.isEmpty()) return emptyList()
        save()
        for (s in parsed) reconcileOAuth(s)
        return parsed
    }

    private fun reconcileOAuth(server: MCPServerConfig) {
        runCatching {
            com.rikkaminis.app.mcp.oauth.MCPOAuthStore.reconcileForServer(
                context = context,
                server = server.id,
                urlTransport = persistsAsUrlTransport(server),
                oauth = server.oauth,
                serverUrl = server.url,
            )
        }.onFailure { Log.w(TAG, "OAuth reconcile failed") }
    }
}
"""

# crack 1, the exact pre-fix shape: a "cleanup" that only drops the secret
BAD_STORE_SECRET_ONLY = GOOD_STORE.replace(
    """    fun setClientSecret(context: Context, server: String, secret: String?) {""",
    """    fun clearOAuth(context: Context, server: String) {
        prefs(context).edit().remove(secretKey(server)).apply()
    }

    fun setClientSecret(context: Context, server: String, secret: String?) {""",
)

# purge outcome silently dropped from the decision
BAD_STORE_NO_PURGE = GOOD_STORE.replace(
    "        !usableOAuth(urlTransport, oauth) -> ReconcileAction.PURGE",
    "        !usableOAuth(urlTransport, oauth) -> ReconcileAction.KEEP",
)

# tokens forgotten, bridge file left in the guest
BAD_STORE_NO_BRIDGE_DELETE = GOOD_STORE.replace(
    """    fun signOut(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).apply()
        deleteBridgeFile(context, server)
    }""",
    """    fun signOut(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).apply()
    }""",
)

# reconcile degenerated into purge-only (crack 2 re-opens)
BAD_STORE_NO_MATERIALIZE = GOOD_STORE.replace(
    "                materializeBridgeFile(context, server, BridgePayload(accessToken = stored!!.accessToken, tokenEndpoint = cfg.tokenEndpoint))\n",
    "",
)

BAD_REPO_ADD_UNWIRED = GOOD_REPO.replace("        reconcileOAuth(server)\n        return true\n    }\n\n    fun update(",
                                         "        return true\n    }\n\n    fun update(")
BAD_REPO_UPDATE_UNWIRED = GOOD_REPO.replace("""        _servers.value = _servers.value.map { if (it.id == server.id) server else it }
        save()
        reconcileOAuth(server)
        return true""", """        _servers.value = _servers.value.map { if (it.id == server.id) server else it }
        save()
        return true""")
BAD_REPO_IMPORT_UNWIRED = GOOD_REPO.replace("        for (s in parsed) reconcileOAuth(s)\n", "")
BAD_REPO_HELPER_DEAD = GOOD_REPO.replace(
    "            com.rikkaminis.app.mcp.oauth.MCPOAuthStore.reconcileForServer(",
    "            com.rikkaminis.app.mcp.oauth.MCPOAuthStore.clientSecret(",
)
BAD_REPO_SECRET_VERB = GOOD_REPO.replace(
    "    private fun reconcileOAuth(server: MCPServerConfig) {",
    """    private fun legacyClear(server: MCPServerConfig) {
        com.rikkaminis.app.mcp.oauth.MCPOAuthStore.setClientSecret(context, server.id, null)
    }

    private fun reconcileOAuth(server: MCPServerConfig) {""",
)

# ── [da1029-E] UI fixtures ────────────────────────────────────────────────
GOOD_UI = """
@Composable
fun MCPIntegrationsScreen(mcpRepository: MCPRepository) {
    val servers by mcpRepository.servers.collectAsState()
    var editServer by remember { mutableStateOf<MCPRepository.MCPServerConfig?>(null) }
    if (editServer != null) {
        editServer?.let { captured ->
            val liveServer = servers.find { it.id == captured.id } ?: captured
            LaunchedEffect(captured.id, servers) {
                if (servers.none { it.id == captured.id }) editServer = null
            }
            MCPAddSheet(
                mcpRepository = mcpRepository,
                editServer = liveServer,
                onDismiss = { editServer = null },
                onRequestDelete = { deleteId = liveServer.id },
            )
        }
    }
}

@Composable
private fun MCPFormTab(editServer: MCPRepository.MCPServerConfig?) {
    var transport by remember(editServer) { mutableIntStateOf(if (editServer?.isStdio == true) 2 else 0) }
    var name by remember(editServer) { mutableStateOf(editServer?.id ?: "") }
    var oauthClientSecret by remember(editServer) {
        mutableStateOf(editServer?.id?.let { MCPOAuthStore.clientSecret(context, it) } ?: "")
    }
    var errorText by remember { mutableStateOf<String?>(null) }
    fun save() {
        if (isEdit && servers.value.none { it.id == server.id }) {
            AppLogger.warning("MCPSheet", "deleted while open; save refused")
            return
        }
        com.rikkaminis.app.mcp.oauth.MCPOAuthStore.setClientSecret(
            context, server.id, null,
        )
    }
}
"""

# one field left un-keyed (the "15 of 16" shape)
BAD_UI_UNKEYED = GOOD_UI.replace(
    '    var name by remember(editServer) { mutableStateOf(editServer?.id ?: "") }',
    '    var name by remember { mutableStateOf(editServer?.id ?: "") }',
)

# the pre-fix shape: the sheet gets the tap-time snapshot
BAD_UI_SNAPSHOT = GOOD_UI.replace(
    "            val liveServer = servers.find { it.id == captured.id } ?: captured\n",
    "",
).replace("editServer = liveServer,", "editServer = captured,")

# live value resolved but never passed through
BAD_UI_NOT_PASSED = GOOD_UI.replace("editServer = liveServer,", "editServer = captured,")

# [dv14-B3] the orphan-credential shape: elvis keeps the sheet in edit mode and
# the close-on-disappear effect is gone — Save writes setClientSecret under a
# deleted id while update() returns false, silently dropped
BAD_UI_ORPHAN = GOOD_UI.replace(
    """            LaunchedEffect(captured.id, servers) {
                if (servers.none { it.id == captured.id }) editServer = null
            }
""",
    "",
)

# resolved without the elvis fallback AND without the close effect (deleted
# entry ⇒ add flow AND orphan credentials)
BAD_UI_NO_FALLBACK = BAD_UI_ORPHAN.replace(
    "servers.find { it.id == captured.id } ?: captured",
    "servers.find { it.id == captured.id }",
)

# [dv14-B3] U3: the form stores credentials but never checks the listing still
# contains the id (the presence guard is gone)
BAD_UI_NO_REFUSE = GOOD_UI.replace(
    """        if (isEdit && servers.value.none { it.id == server.id }) {
            AppLogger.warning("MCPSheet", "deleted while open; save refused")
            return
        }
""",
    "",
)

# the form is gone entirely (anchor drift)
BAD_UI_NO_FORM = GOOD_UI.replace("private fun MCPFormTab(", "private fun MCPFormBody(")

# the comment trap: docs describing the old shape / claiming the fix
COMMENTED_UI = GOOD_UI.replace(
    "private fun MCPFormTab(",
    """// the old shape was `var name by remember { mutableStateOf(editServer?.id ?: "") }`
private fun MCPFormTab(""",
)
LYING_UI = BAD_UI_UNKEYED.replace(
    "private fun MCPFormTab(",
    """// every field is now keyed on editServer — no un-keyed remember remains
private fun MCPFormTab(""",
)


# -- [da1034-A1] artifact-backup fixtures ------------------------------------

GOOD_SCOPE = """
object ArtifactBackupScope {
    private val EXCLUDED_DIRS = setOf("sandbox-env")
    private val DENIED_DIRS = setOf("oauth")

    private fun walk(dir: java.io.File, prefix: String, out: MutableList<String>) {
        val children = dir.listFiles() ?: return
        for (child in children.sortedBy { it.name }) {
            if (child.isDirectory) {
                if (child.name in DENIED_DIRS) continue
                if (prefix.isEmpty() && child.name in EXCLUDED_DIRS) continue
                walk(child, prefix + "/" + child.name, out)
                continue
            }
            out.add(prefix + "/" + child.name)
        }
    }
}
"""
DENY_VAL = "    private val DENIED_DIRS = setOf(\"oauth\")\n"
DENY_GUARD = "                if (child.name in DENIED_DIRS) continue\n"
BAD_SCOPE_NO_DENY = GOOD_SCOPE.replace(DENY_VAL, "").replace(DENY_GUARD, "")
BAD_SCOPE_DEAD_DENY = GOOD_SCOPE.replace(DENY_GUARD, "")
BAD_SCOPE_COMMENT_ONLY = GOOD_SCOPE.replace(DENY_VAL, "// DENIED_DIRS was here\n").replace(DENY_GUARD, "")

GOOD_BACKUP = """
object ConfigBackup {
    suspend fun import(tmp: java.io.File, artifactRoots: List<java.io.File>?, skipped: ArrayList<String>) {
        val destByPrefix = HashMap<String, java.io.File>()
        for (root in artifactRoots!!) {
            val prefix = if (root.name == "mcp-servers") "mcp" else "shared"
            destByPrefix[prefix] = root
        }
        val filtered = java.io.File.createTempFile("artifact-restore-", ".zip")
        try {
            val oauthDropped = stripMcpOAuthEntries(tmp, filtered)
            if (oauthDropped > 0) {
                skipped.add("artifacts: skipped $oauthDropped oauth bridge entries")
            }
            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)
        } finally {
            runCatching { filtered.delete() }
        }
    }

    internal fun stripMcpOAuthEntries(src: java.io.File, dst: java.io.File): Int {
        if (isMcpOAuthEntryName(src.name)) return 0
        return 1
    }

    internal fun isMcpOAuthEntryName(name: String): Boolean {
        val segs = ArrayList<String>(4)
        for (seg in name.split('/')) {
            if (seg.isEmpty() || seg == ".") continue
            segs.add(seg)
        }
        return segs.size >= 2 && segs[0] == "mcp" && segs[1] == "oauth"
    }
}
"""
WIRED = """            val oauthDropped = stripMcpOAuthEntries(tmp, filtered)
            if (oauthDropped > 0) {
                skipped.add("artifacts: skipped $oauthDropped oauth bridge entries")
            }
            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)"""
BAD_BACKUP_NO_WIRING = GOOD_BACKUP.replace(WIRED, "            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(tmp, destByPrefix)")
BAD_BACKUP_AFTER_EXTRACT = GOOD_BACKUP.replace(
    WIRED,
    """            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)
            val oauthDropped = stripMcpOAuthEntries(tmp, filtered)
            if (oauthDropped > 0) {
                skipped.add("artifacts: skipped $oauthDropped oauth bridge entries")
            }""")
BAD_BACKUP_HOLLOW_PREDICATE = GOOD_BACKUP.replace(
    'segs[0] == "mcp" && segs[1] == "oauth"', 'segs[0] == "mcp" && segs[1] == "nope"')
BAD_BACKUP_NO_REPORT = GOOD_BACKUP.replace(
    '                skipped.add("artifacts: skipped $oauthDropped oauth bridge entries")\n', "")
# [da1034-A1] 变异 m2 的形状：filter 仍在、顺序也对，但抽取吃的是原始 tmp。
BAD_BACKUP_EXTRACT_RAW = GOOD_BACKUP.replace(
    "ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)",
    "ArtifactArchiver.extractZipByPrefix(tmp, destByPrefix)")

# -- [dv20-a1 A-01] artifact-face fixtures -----------------------------------

GOOD_PACK = """
object ConfigBackup {
    suspend fun buildSections(artifactRoots: List<java.io.File>?, includeSecrets: Boolean) {
        if (artifactRoots != null && artifactRoots.isNotEmpty()) {
            for (root in artifactRoots) {
                val isMcpRoot = root.name == "mcp-servers"
                val prefix = if (isMcpRoot) "mcp" else "shared"
                val transform =
                    if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null
                val dropped = ArrayList<String>()
                totalBytes += ArtifactArchiver.writeEntries(
                    zos, root, sel.files, prefix + "/", transform, dropped,
                )
                fileCount += sel.files.size - dropped.size
                artifactSkipped += dropped.size
            }
        }
    }

    internal fun redactMcpArtifactEntry(relPath: String, bytes: ByteArray): ByteArray? {
        if (relPath != "servers.json") return bytes
        return try {
            val obj = org.json.JSONObject(String(bytes))
            stripExportedMcpSecrets(obj)
            obj.toString(2).toByteArray()
        } catch (t: Throwable) {
            null
        }
    }

    internal fun stripExportedMcpSecrets(obj: org.json.JSONObject) { }
}
"""

BAD_PACK_NO_REDACTOR = GOOD_PACK.replace(
    "if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null",
    "null")
BAD_PACK_UNGATED = GOOD_PACK.replace(
    "if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null",
    "if (isMcpRoot) ::redactMcpArtifactEntry else null")
BAD_PACK_NO_STRIP = GOOD_PACK.replace("            stripExportedMcpSecrets(obj)\n", "")
BAD_PACK_INLINE_RULE = GOOD_PACK.replace(
    "            stripExportedMcpSecrets(obj)\n",
    '            val section = obj.optJSONObject("headers")\n'
    '            if (section != null) for (k in section.keys()) section.put(k, "")\n')
BAD_PACK_NO_FAILCLOSED = GOOD_PACK.replace(
    "        } catch (t: Throwable) {\n            null\n        }",
    "        } catch (t: Throwable) {\n            bytes\n        }")
BAD_PACK_NO_ROOT = GOOD_PACK.replace('val isMcpRoot = root.name == "mcp-servers"', 'val isMcp = 1')

GOOD_ARCHIVER = """
object ArtifactArchiver {
    fun writeEntries(
        zos: java.util.zip.ZipOutputStream,
        root: java.io.File,
        files: List<ArtifactBackupScope.SelectedFile>,
        entryPrefix: String = "",
        transform: ((String, ByteArray) -> ByteArray?)? = null,
        droppedSink: MutableList<String>? = null,
    ): Long {
        var totalBytes = 0L
        for (f in files) {
            val src = java.io.File(root, f.relPath)
            if (!src.isFile) continue
            var bytes = src.readBytes()
            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
            zos.putNextEntry(java.util.zip.ZipEntry(entryPrefix + f.relPath))
            zos.write(bytes)
            zos.closeEntry()
            totalBytes += bytes.size
        }
        return totalBytes
    }
}
"""

ARCHIVER_DROP = """                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
"""
BAD_ARCHIVER_NO_NULL_CHECK = GOOD_ARCHIVER.replace(ARCHIVER_DROP, "")
BAD_ARCHIVER_NO_SINK = GOOD_ARCHIVER.replace("                    droppedSink?.add(f.relPath)\n", "")
BAD_ARCHIVER_NO_TRANSFORM = GOOD_ARCHIVER.replace(
    """            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
""", "")
BAD_ARCHIVER_NO_FUN = GOOD_ARCHIVER.replace("fun writeEntries(", "fun writeEntriesX(")


# [dv25-A1] fixtures for check_config: MCPOAuthConfig's parse/write key
# contract. GOOD mirrors the shipped shape; each BAD trips exactly one
# criterion group.
GOOD_CLI = '''
        oauth = {
            "mode": "static",
            "clientId": oauth_client_id,
            "authorizationEndpoint": oauth_auth_endpoint,
            "tokenEndpoint": oauth_token_endpoint,
        }
        if oauth_scopes:
            oauth["scopes"] = oauth_scopes
        if oauth_redirect:
            oauth["redirectURI"] = oauth_redirect
'''

CLI_WITH_EXTRA_KEY = GOOD_CLI + '''
        if oauth_device_code:
            oauth["deviceAuthorizationEndpoint"] = oauth_device_code
'''

GOOD_CONFIG = """
data class MCPOAuthConfig(
    val mode: String = "static",
    val clientId: String = "",
    val authorizationEndpoint: String = "",
    val tokenEndpoint: String = "",
    val scopes: String? = null,
    val redirectUri: String? = null,
) {
    val isConfigured: Boolean
        get() = clientId.isNotBlank() &&
            authorizationEndpoint.isNotBlank() &&
            tokenEndpoint.isNotBlank()

    fun toJson(): JSONObject = JSONObject().apply {
        put("mode", mode)
        put("client_id", clientId)
        put("authorization_endpoint", authorizationEndpoint)
        put("token_endpoint", tokenEndpoint)
        scopes?.takeIf { it.isNotBlank() }?.let { put("scopes", it) }
        redirectUri?.takeIf { it.isNotBlank() }?.let { put("redirect_uri", it) }
    }

    companion object {
        private val CLIENT_ID_KEYS = listOf("client_id", "clientId")
        private val AUTH_ENDPOINT_KEYS = listOf("authorization_endpoint", "authorizationEndpoint")
        private val TOKEN_ENDPOINT_KEYS = listOf("token_endpoint", "tokenEndpoint")
        private val REDIRECT_URI_KEYS = listOf("redirect_uri", "redirectURI")

        private fun JSONObject.firstNonBlank(keys: List<String>): String =
            keys.firstNotNullOfOrNull { key -> optString(key, "").ifBlank { null } } ?: ""

        fun fromJson(obj: JSONObject?): MCPOAuthConfig? {
            if (obj == null) return null
            val config = MCPOAuthConfig(
                mode = obj.optString("mode", "static").ifBlank { "static" },
                clientId = obj.firstNonBlank(CLIENT_ID_KEYS),
                authorizationEndpoint = obj.firstNonBlank(AUTH_ENDPOINT_KEYS),
                tokenEndpoint = obj.firstNonBlank(TOKEN_ENDPOINT_KEYS),
                scopes = obj.optString("scopes", "").ifBlank { null },
                redirectUri = obj.firstNonBlank(REDIRECT_URI_KEYS).ifBlank { null },
            )
            return if (config.clientId.isBlank() &&
                config.authorizationEndpoint.isBlank() &&
                config.tokenEndpoint.isBlank()
            ) {
                null
            } else {
                config
            }
        }
    }
}
"""

# C1 — the alias reads are gone (the shipped-but-blank state).
BAD_CONFIG_NO_ALIAS = GOOD_CONFIG.replace(
    'listOf("client_id", "clientId")', 'listOf("client_id")').replace(
    'listOf("authorization_endpoint", "authorizationEndpoint")', 'listOf("authorization_endpoint")').replace(
    'listOf("token_endpoint", "tokenEndpoint")', 'listOf("token_endpoint")').replace(
    'listOf("redirect_uri", "redirectURI")', 'listOf("redirect_uri")')

# C1 — the pre-fix file, verbatim in shape: no tables at all, snake_case
# literals only (the four CLI-written fields parse blank).
BAD_CONFIG_BASELINE = """
data class MCPOAuthConfig(
    val mode: String = "static",
    val clientId: String = "",
    val authorizationEndpoint: String = "",
    val tokenEndpoint: String = "",
    val scopes: String? = null,
    val redirectUri: String? = null,
) {
    val isConfigured: Boolean
        get() = clientId.isNotBlank() &&
            authorizationEndpoint.isNotBlank() &&
            tokenEndpoint.isNotBlank()

    fun toJson(): JSONObject = JSONObject().apply {
        put("mode", mode)
        put("client_id", clientId)
        put("authorization_endpoint", authorizationEndpoint)
        put("token_endpoint", tokenEndpoint)
        scopes?.takeIf { it.isNotBlank() }?.let { put("scopes", it) }
        redirectUri?.takeIf { it.isNotBlank() }?.let { put("redirect_uri", it) }
    }

    companion object {
        fun fromJson(obj: JSONObject?): MCPOAuthConfig? {
            if (obj == null) return null
            val config = MCPOAuthConfig(
                mode = obj.optString("mode", "static").ifBlank { "static" },
                clientId = obj.optString("client_id", ""),
                authorizationEndpoint = obj.optString("authorization_endpoint", ""),
                tokenEndpoint = obj.optString("token_endpoint", ""),
                scopes = obj.optString("scopes", "").ifBlank { null },
                redirectUri = obj.optString("redirect_uri", "").ifBlank { null },
            )
            return if (config.clientId.isBlank() &&
                config.authorizationEndpoint.isBlank() &&
                config.tokenEndpoint.isBlank()
            ) null else config
        }
    }
}
"""

# C1 — a mention of the CLI spelling inside a COMMENT cannot satisfy the check.
BAD_CONFIG_COMMENT_ONLY = BAD_CONFIG_BASELINE.replace(
    "    companion object {",
    "    // The in-guest CLI spells these clientId / authorizationEndpoint /\n"
    "    // tokenEndpoint — honoured the same way as parseStartupTimeout does.\n"
    "    companion object {")

# C2 — alias promoted to primary (camelCase only, no snake_case fallback).
BAD_CONFIG_ALIAS_FIRST = GOOD_CONFIG.replace(
    'listOf("client_id", "clientId")', 'listOf("clientId")').replace(
    'listOf("authorization_endpoint", "authorizationEndpoint")', 'listOf("authorizationEndpoint")').replace(
    'listOf("token_endpoint", "tokenEndpoint")', 'listOf("tokenEndpoint")').replace(
    'listOf("redirect_uri", "redirectURI")', 'listOf("redirectURI")')

# C2 — table reversed: camelCase first, snake_case as the fallback.
BAD_CONFIG_TABLE_REVERSED = GOOD_CONFIG.replace(
    'listOf("client_id", "clientId")', 'listOf("clientId", "client_id")')

# C3 — the tables exist but fromJson reads literals: a fallback on paper only.
BAD_CONFIG_TABLE_UNWIRED = GOOD_CONFIG.replace(
    "clientId = obj.firstNonBlank(CLIENT_ID_KEYS)", 'clientId = obj.optString("client_id", "")')

# C4 — the read side learned a second spelling and the write side followed.
BAD_CONFIG_TOJSON_CAMEL = GOOD_CONFIG.replace(
    'put("client_id", clientId)', 'put("clientId", clientId)')

# C5 — the "no endpoints is noise" null path removed.
BAD_CONFIG_NO_NULL_GUARD = GOOD_CONFIG.replace(
    """            return if (config.clientId.isBlank() &&
                config.authorizationEndpoint.isBlank() &&
                config.tokenEndpoint.isBlank()
            ) {
                null
            } else {
                config
            }""",
    "            return config")

def selftest():
    failures = []

    def expect_clean(name, result):
        if result:
            failures.append("%s must be clean, got %s" % (name, result))

    def expect_trip(name, result, needle):
        if not any(needle in item for item in result):
            failures.append("%s did not trip '%s' (got %s)" % (name, needle, result))

    good_store = check_store(mask(GOOD_STORE))
    good_repo = check_repo(mask(GOOD_REPO))
    expect_clean("compliant store fixture", good_store)
    expect_clean("compliant repo fixture", good_repo)

    s_secret = check_store(mask(BAD_STORE_SECRET_ONLY))
    expect_trip("secret-only cleanup fixture", s_secret, "is not a cleanup")

    s_purge = check_store(mask(BAD_STORE_NO_PURGE))
    expect_trip("purge-outcome-dropped fixture", s_purge, "never returns PURGE")

    s_bridge = check_store(mask(BAD_STORE_NO_BRIDGE_DELETE))
    expect_trip("tokens-without-bridge-delete fixture", s_bridge, "leaves the bridge file")

    s_mat = check_store(mask(BAD_STORE_NO_MATERIALIZE))
    expect_trip("purge-only reconcile fixture", s_mat, "does not call `materializeBridgeFile(")

    r_add = check_repo(mask(BAD_REPO_ADD_UNWIRED))
    expect_trip("unwired add fixture", r_add, "`add` does not reach")

    r_upd = check_repo(mask(BAD_REPO_UPDATE_UNWIRED))
    expect_trip("unwired update fixture", r_upd, "`update` does not reach")

    r_imp = check_repo(mask(BAD_REPO_IMPORT_UNWIRED))
    expect_trip("unwired importJSON fixture", r_imp, "`importJSON` does not reach")

    r_dead = check_repo(mask(BAD_REPO_HELPER_DEAD))
    expect_trip("dead-indirection fixture", r_dead, "does not call `MCPOAuthStore.reconcileForServer(")

    r_secret = check_repo(mask(BAD_REPO_SECRET_VERB))
    expect_trip("repository-secret-only-verb fixture", r_secret, "uses `setClientSecret(")

    # [da1034-A1] artifact-backup arms
    good_scope = check_backup_scope(mask_comments(GOOD_SCOPE))
    expect_clean("compliant artifact scope fixture", good_scope)

    b_nodeny = check_backup_scope(mask_comments(BAD_SCOPE_NO_DENY))
    expect_trip("scope without deny-set", b_nodeny, 'no `<val> = setOf("oauth")` deny-set')

    b_dead = check_backup_scope(mask_comments(BAD_SCOPE_DEAD_DENY))
    expect_trip("scope with an inert deny-set", b_dead, "the walk never skips on it")

    b_comment = check_backup_scope(mask_comments(BAD_SCOPE_COMMENT_ONLY))
    expect_trip("scope whose deny-set is only a comment", b_comment,
                'no `<val> = setOf("oauth")` deny-set')

    good_backup = check_backup_import(mask_comments(GOOD_BACKUP), mask(GOOD_BACKUP))
    expect_clean("compliant backup import fixture", good_backup)

    b_nowire = check_backup_import(mask_comments(BAD_BACKUP_NO_WIRING), mask(BAD_BACKUP_NO_WIRING))
    expect_trip("import extracting the raw archive", b_nowire, "never calls `stripMcpOAuthEntries(`")

    b_after = check_backup_import(mask_comments(BAD_BACKUP_AFTER_EXTRACT), mask(BAD_BACKUP_AFTER_EXTRACT))
    expect_trip("filter running after extraction", b_after, "runs AFTER")

    b_hollow = check_backup_import(mask_comments(BAD_BACKUP_HOLLOW_PREDICATE), mask(BAD_BACKUP_HOLLOW_PREDICATE))
    expect_trip("predicate that matches nothing", b_hollow, "must pin BOTH")

    b_noreport = check_backup_import(mask_comments(BAD_BACKUP_NO_REPORT), mask(BAD_BACKUP_NO_REPORT))
    expect_trip("silent oauth drop", b_noreport, "must reach `skipped`")

    # [da1034-A1] 变异 m2 的形状：filter 在、顺序对，但抽取吃的是原始 tmp
    b_raw = check_backup_import(mask_comments(BAD_BACKUP_EXTRACT_RAW), mask(BAD_BACKUP_EXTRACT_RAW))
    expect_trip("extraction consuming the raw archive", b_raw,
                "extracts something other than the filtered archive")

    # [da1029-E] UI arms
    good_ui, good_ui_keyed = check_ui(mask(GOOD_UI))
    expect_clean("compliant live-edit UI fixture", good_ui)

    u_unkeyed, _ = check_ui(mask(BAD_UI_UNKEYED))
    expect_trip("un-keyed field fixture", u_unkeyed, "has no key on that entry")

    u_snap, _ = check_ui(mask(BAD_UI_SNAPSHOT))
    expect_trip("tap-time snapshot fixture", u_snap, "the sheet is handed the tap-time")

    u_pass, _ = check_ui(mask(BAD_UI_NOT_PASSED))
    expect_trip("live value not passed fixture", u_pass, "is resolved but never passed")

    u_fall, _ = check_ui(mask(BAD_UI_NO_FALLBACK))
    expect_trip("no elvis fallback fixture", u_fall, "has no `?: ` fallback")

    u_form, _ = check_ui(mask(BAD_UI_NO_FORM))
    expect_trip("renamed form fixture", u_form, "field-state keys this check pins is gone")

    # [dv14-B3] close-on-disappear arms
    u_orphan, _ = check_ui(mask(BAD_UI_ORPHAN))
    expect_trip("orphan-credential fixture (elvis, no close effect)", u_orphan,
                "no close-on-disappear effect was found")

    u_orphan_noelvis, _ = check_ui(mask(BAD_UI_NO_FALLBACK))
    expect_trip("no-fallback fixture (no elvis, no close effect)", u_orphan_noelvis,
                "has no `?: ` fallback")

    # [dv14-B3] U3 arms
    u_norefuse, _ = check_ui(mask(BAD_UI_NO_REFUSE))
    expect_trip("credential store without presence guard", u_norefuse,
                "never checks the listing still contains the id")

    expect_clean("comment describing the old shape",
                 check_ui(mask(COMMENTED_UI))[0])
    expect_trip("comment claiming a fix", check_ui(mask(LYING_UI))[0], "has no key on that entry")

    # masking arm: a compliant store whose *comment* mentions the old
    # secret-only cleanup, and a violating store whose comment claims it is
    # fixed, must be judged on the code only.
    commented = GOOD_STORE.replace(
        "    internal enum class ReconcileAction { PURGE, KEEP, REFRESH_BRIDGE }",
        "    // we used to only remove(secretKey(server)) here — now purged\n"
        "    internal enum class ReconcileAction { PURGE, KEEP, REFRESH_BRIDGE }",
    )
    expect_clean("comment mentioning the old cleanup", check_store(mask(commented)))
    lying = BAD_STORE_SECRET_ONLY.replace(
        "    fun clearOAuth(context: Context, server: String) {",
        "    // removed only the secret before; now it is a full purge\n"
        "    fun clearOAuth(context: Context, server: String) {",
    )
    expect_trip("comment claiming a fix", check_store(mask(lying)), "is not a cleanup")

    print("selftest compliant store        -> %d violation(s)" % len(good_store))
    print("selftest compliant repo         -> %d violation(s)" % len(good_repo))
    print("selftest secret-only cleanup    -> %d violation(s)" % len(s_secret))
    print("selftest purge outcome dropped  -> %d violation(s)" % len(s_purge))
    print("selftest tokens keep bridge     -> %d violation(s)" % len(s_bridge))
    print("selftest purge-only reconcile   -> %d violation(s)" % len(s_mat))
    print("selftest unwired add/update/imp -> %d/%d/%d violation(s)" % (len(r_add), len(r_upd), len(r_imp)))
    print("selftest dead indirection       -> %d violation(s)" % len(r_dead))
    print("selftest repo secret-only verb  -> %d violation(s)" % len(r_secret))
    print("selftest UI live-edit           -> %d violation(s), %d field state(s) keyed"
          % (len(good_ui), good_ui_keyed))
    print("selftest UI arms (unkeyed/snapshot/not-passed/no-fallback/renamed-form) -> %d/%d/%d/%d/%d violation(s)"
          % (len(u_unkeyed), len(u_snap), len(u_pass), len(u_fall), len(u_form)))
    print("selftest dv14-B3 arms (orphan/no-fallback-no-close/no-refuse-guard) -> %d/%d/%d violation(s)"
          % (len(u_orphan), len(u_orphan_noelvis), len(u_norefuse)))
    print("selftest oauth-backup arms (deny-set/dead-deny/comment-only/no-wiring/after-extract/hollow-pred/silent-drop/extract-raw) -> %d/%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (len(b_nodeny), len(b_dead), len(b_comment), len(b_nowire), len(b_after),
             len(b_hollow), len(b_noreport), len(b_raw)))

    # [dv20-a1 A-01] artifact-face arms: each fixture must red exactly one group
    def expect_only(name, groups, gi, needle):
        if not any(needle in x for x in groups[gi]):
            failures.append(
                "%s must trip group %d of 3 with '%s', got %s"
                % (name, gi + 1, needle, [len(g) for g in groups]))
            return
        for j, g in enumerate(groups):
            if j != gi and g:
                failures.append("%s left sibling group %d dirty: %s" % (name, j + 1, g))

    def art(fixture=None, archiver=None):
        p = fixture if fixture is not None else GOOD_PACK
        a = archiver if archiver is not None else GOOD_ARCHIVER
        return check_artifact_redaction(mask_comments(p), mask(p), mask(a))

    p_good, r_good, a_good = art()
    expect_clean("compliant credential-free pack fixture", p_good)
    expect_clean("compliant redactor fixture", r_good)
    expect_clean("compliant archiver fixture", a_good)

    expect_only("pack without a redactor fixture", art(BAD_PACK_NO_REDACTOR), 0, "is handed no")
    expect_only("ungated redactor fixture", art(BAD_PACK_UNGATED), 0, "not gated on")
    expect_only("pack anchor drift fixture", art(BAD_PACK_NO_ROOT), 0, "re-anchor this gate")
    expect_only("redactor without the Stage 5 strip fixture", art(BAD_PACK_NO_STRIP), 1,
                "does not call")
    expect_only("redactor with a second rule fixture", art(BAD_PACK_INLINE_RULE), 1,
                "blanks secret values itself")
    expect_only("redactor that cannot fail closed fixture", art(BAD_PACK_NO_FAILCLOSED), 1,
                "parse-failure path")
    expect_only("archiver that never drops fixture", art(archiver=BAD_ARCHIVER_NO_NULL_CHECK), 2,
                "never compared to null")
    expect_only("archiver that does not report a drop fixture", art(archiver=BAD_ARCHIVER_NO_SINK), 2,
                "droppedSink")
    expect_only("archiver that ignores its transform fixture",
                art(archiver=BAD_ARCHIVER_NO_TRANSFORM), 2, "never applies its transform")
    expect_only("archiver anchor drift fixture", art(archiver=BAD_ARCHIVER_NO_FUN), 2,
                "re-anchor this gate")

    print("selftest dv20-a1 artifact arms (no-redactor/ungated/pack-rot/no-strip/second-rule/"
          "no-fail-closed/drop-removed/no-sink/ignores-transform/archiver-rot) -> "
          "%d/%d/%d/%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (len(art(BAD_PACK_NO_REDACTOR)[0]), len(art(BAD_PACK_UNGATED)[0]),
             len(art(BAD_PACK_NO_ROOT)[0]), len(art(BAD_PACK_NO_STRIP)[1]),
             len(art(BAD_PACK_INLINE_RULE)[1]), len(art(BAD_PACK_NO_FAILCLOSED)[1]),
             len(art(archiver=BAD_ARCHIVER_NO_NULL_CHECK)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_SINK)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_TRANSFORM)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_FUN)[2])))



    # [dv25-A1] parse/write key-contract arms: each fixture must trip exactly
    # one criterion group, so a green run proves the five checks all have teeth.
    N1 = "does not read"
    N2A = "not ordered primary-then-alias"
    N2B = "reads only"
    N3 = "never consults it"
    N4 = "camelCase key"
    N5 = "no longer treats a blank"

    def cfg_arm(label, code, needle, cli=GOOD_CLI):
        res = check_config(mask_comments(code), cli)
        expect_trip(label, res, needle)
        for item in res:
            if needle not in item:
                failures.append("%s tripped an unexpected criterion: %s" % (label, item))
        return len(res)

    cfg_clean = check_config(mask_comments(GOOD_CONFIG), GOOD_CLI)
    expect_clean("compliant MCPOAuthConfig fixture", cfg_clean)
    if cfg_clean:
        failures.append("compliant config fixture tripped: %s" % cfg_clean)

    a1 = cfg_arm("alias-reads-removed fixture", BAD_CONFIG_NO_ALIAS, N1)
    a2 = cfg_arm("baseline-snake-only fixture", BAD_CONFIG_BASELINE, N1)
    a3 = cfg_arm("comment-only-alias fixture", BAD_CONFIG_COMMENT_ONLY, N1)
    a4 = cfg_arm("alias-promoted-to-primary fixture", BAD_CONFIG_ALIAS_FIRST, N2B)
    a5 = cfg_arm("table-reversed fixture", BAD_CONFIG_TABLE_REVERSED, N2A)
    a6 = cfg_arm("table-declared-but-unused fixture", BAD_CONFIG_TABLE_UNWIRED, N3)
    a7 = cfg_arm("toJson-writes-camelCase fixture", BAD_CONFIG_TOJSON_CAMEL, N4)
    a8 = cfg_arm("null-path-removed fixture", BAD_CONFIG_NO_NULL_GUARD, N5)
    a9 = cfg_arm("new-CLI-key fixture", GOOD_CONFIG, N1, cli=CLI_WITH_EXTRA_KEY)

    # fail-closed: an unreadable CLI (no derived key set) must not pass silently.
    no_cli = check_config(mask_comments(GOOD_CONFIG), "")
    expect_trip("empty CLI source", no_cli, "cannot derive the in-guest CLI")
    no_from = check_config(mask_comments("class X { fun toJson(): JSONObject = JSONObject() }"), GOOD_CLI)
    expect_trip("fromJson gone", no_from, "no `fun fromJson(`")

    print("selftest dv25-A1 config arms (no-alias/baseline/comment-only/alias-first/"
          "reversed/unwired/tojson-camel/no-null/new-cli-key/no-cli/no-fun) -> "
          "%d/%d/%d/%d/%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (a1, a2, a3, a4, a5, a6, a7, a8, a9, len(no_cli), len(no_from)))


    if failures:
        for item in failures:
            print("SELFTEST FAIL " + item)
        return 1
    print(SELFTEST_MARK)
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())
