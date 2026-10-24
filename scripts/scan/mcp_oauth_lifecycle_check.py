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

[dv28-a5 A-05] Two more criteria extend the SAME artifact stage to a second
credential-free file that was written twice. Stage 7 merges the backup's
`mcpServers` into this device's list (`MCPRepository.importJSON` keeps the
rows the backup does not carry, then saves); Stage 9 then restored
`mcp/servers.json` out of the archive VERBATIM, replacing the merged file with
the backup's list. Every server the target device had and the backup did not
was erased on disk — the in-memory merge hid it until the next cold start
re-read the file (`ConfigBackup` only reloads `skillRepo`).

  C-ST9  `import` must not copy `mcp/servers.json` when Stage 7 merged the
      list. Derived, not enumerated: the operands of the `if (…)` that gates
      the Stage 7 merge call are the operands the skip has to be derived from,
      so the filter call carries a third argument — an identifier computed in
      `import` from at least two of them. `true` (or a flag that ignores the
      repository) is not a decision: an unconditional drop loses the list from
      an archive-only backup (no `mcpServers` section), an unconditional copy
      erases the merged one. The predicate must pin the ONE entry name
      ("mcp" + "servers.json", segments collapsed) and the filter must consult
      it only behind that switch; the `mcp` prefix must stay an extraction
      destination so the rest of the MCP data still restores.
  C-ST9b Two legal shapes, whitelisted so the criterion cannot lock one
      implementation: (a) re-import after extraction and call
      `reloadFromDisk()` after it, or (b) skip the entry and say so — a
      `skipped` literal naming the file and the stage that restored it.
      A comment cannot satisfy either (strings are masked for the structural
      half; the literal half reads the scoped source, and the arm that only
      comments the drop out is still red on C-ST9).

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

[dv26-A2] The BRIDGE side of the same credential invariant, in the direction
nobody implemented (deep-audit-1046 S-02). `MCPOAuthStore` and
`/var/minis/mcp-servers/oauth/<server>.json` are two copies of one credential:
the host writes the bridge, and the in-guest CLI rewrites it on every refresh
(`transport/http.py::_oauth_refresh → _save_oauth_tokens`) with a new
access_token / expires_at and — under refresh-token rotation — a new
refresh_token. Nothing read it back, so `REFRESH_BRIDGE` (every save of any
other field of that server) re-materialized the PRE-refresh snapshot; the guest
then presented a rotated-away refresh_token and got `invalid_grant`.

  C1  the function that materializes the bridge must, through its call
      closure, READ the bridge file (a helper, not a comment) and PERSIST what
      it read (`setTokens(`) — reading without persisting just moves the
      regression one save later.
  C2  the promotion must ORDER the bridge expiry against the stored copy (an
      older bridge is the shape of "the app authorized after the guest's last
      refresh" — promoting it is a rollback) and compare the tokens themselves
      (a same-second refresh has an identical expiry). The `>=` vs `>` boundary
      is behaviour and lives in `MCPOAuthBridgeMergeTest`, not here.
  C3  the bridge is a token carrier, not a configuration source: the reader must
      not read `client_id` / `token_endpoint` / `client_secret` / `resource`
      (derived as "what materializeBridgeFile writes, minus the token triple").

  A damaged / older / blank bridge promotes NOTHING — clearing valid host
  tokens on a corrupt guest file would be the inverse bug, and a worse one.

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
# [dv27-A3] the `.secret` handoff: the in-guest CLI cannot write the encrypted
# store, so it drops `<server>.secret` next to the token bridge and expects the
# HOST to import it. Before this, only the edit sheet ever imported it, so
# every other reader saw the stale stored value — and `signOut` DELETED the
# file, destroying the only copy of a client secret that cannot be re-derived.
SECRET_IMPORT_CALL = "importPendingClientSecret("
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

# [dv26-A2] deep-audit-1046 S-02 — the guest -> host half of the token bridge.
# The store and `/var/minis/mcp-servers/oauth/<server>.json` are two copies of
# one credential; the in-guest CLI rewrites its copy on every refresh and
# nothing read it back, so a config save materialized the pre-refresh snapshot
# over it (with a rotated-away refresh_token, i.e. invalid_grant).
MERGE_REL = ("src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/"
             "MCPOAuthBridgeMerge.kt")
BRIDGE_READ_CALL = "readText("
BRIDGE_WRITE_CALL = "writeText("
BRIDGE_PATH_MARKERS = ("bridgeFile(", "bridgeDir(")
BRIDGE_TOKEN_KEYS = ("access_token", "refresh_token", "expires_at")
PREFS_TOKEN_READER = "tokens"
PUT_KEY_RE = re.compile(r'\bput\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')
DATA_CLASS_RE = re.compile(r"\bdata\s+class\s+([A-Za-z_]\w*)\s*\(")
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
COMPARISON_RE = re.compile(r"([A-Za-z_][\w.]*)\s*(>=|<=|!=|==|>|<)\s*([A-Za-z_][\w.]*)")
LOCAL_VAL_RE = re.compile(r"\bval\s+([A-Za-z_]\w*)\s*=\s*([^\n]*)")
ORDERING_OPS = (">=", "<=", ">", "<")
INEQUALITY_OPS = ("!=", "==")


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


def _first_pos(body, needle):
    """Index of [needle] in [body], or -1."""
    i = body.find(needle)
    return i


def _imports_pending_secret(code, name, hops=1):
    """True when `fun <name>` (or a callee ONE hop down) calls the handoff
    import. Derived from the call graph, not from the spelling of the body:
    the import may sit in a helper the function delegates to."""
    bodies = {n: b for n, b, _l in functions(code) if b is not None}
    body = bodies.get(name)
    if body is None:
        return False
    if SECRET_IMPORT_CALL in body:
        return True
    if hops <= 0:
        return False
    for m in CALL_RE.finditer(body):
        callee = m.group(1)
        if callee == name:
            continue
        cb = bodies.get(callee)
        if cb is not None and SECRET_IMPORT_CALL in cb:
            return True
    return False


def check_secret_handoff(code):
    """[dv27-A3] C5/C6 — the `.secret` handoff must have exactly one importer
    and must never be lost.

    The in-guest CLI cannot write the encrypted store, so it drops
    `<server>.secret` next to the token bridge and expects the HOST to import
    it. Two defects shared that file:

      * only the edit sheet ever imported it, so `reconcileForServer` wrote
        the STALE stored secret back into the bridge file and the token
        exchange omitted `client_secret` (a confidential client answers 401);
      * `signOut` deleted the bridge file — and with it the only copy of a
        client secret that had never been imported and cannot be re-derived.

    C5 pins the import into the single exit point (`clientSecret`) and
    requires `signOut` to import BEFORE it deletes the bridge file.
    C6 pins the opposite for `purge`: deleting a server must NOT write the
    secret back into the encrypted store (that would resurrect an orphan
    credential for a server the user just removed).
    """
    v = []
    bodies = {n: b for n, b, _l in functions(code) if b is not None}

    # --- C5a: the single exit point imports the pending handoff -------------
    if "clientSecret" not in bodies:
        v.append(
            "MCPOAuthStore.kt: no `fun clientSecret(` — the single exit point "
            "for a server's client secret is gone; every reader would fall "
            "back to the stored value and ignore the CLI's `.secret` handoff "
            "(ROT: update this gate if it was renamed)"
        )
    elif not _imports_pending_secret(code, "clientSecret"):
        v.append(
            "MCPOAuthStore.kt: `clientSecret` does not import the pending "
            "`.secret` handoff — the in-guest CLI seeds a client secret into "
            "`<server>.secret` because it cannot write the encrypted store, "
            "and only the host can move it in. A reader that skips the import "
            "sees the STALE stored value (or none), so the token exchange "
            "omits `client_secret` and a confidential client answers 401"
        )

    # --- C5b: signOut imports before it deletes the bridge file -------------
    signout = bodies.get("signOut")
    if signout is None:
        v.append(
            "MCPOAuthStore.kt: no `fun signOut(` — the sign-out path is gone "
            "(ROT: update this gate if it was renamed)"
        )
    else:
        imp = _first_pos(signout, SECRET_IMPORT_CALL)
        dele = _first_pos(signout, BRIDGE_DELETE)
        if imp < 0:
            v.append(
                "MCPOAuthStore.kt: `signOut` does not import the pending "
                "`.secret` handoff — it deletes the bridge file, and with it "
                "the only copy of a client secret that was never imported and "
                "cannot be re-derived. `signOut` promises to KEEP the client "
                "secret; a credential loss is not a cleanup gap"
            )
        elif dele >= 0 and imp > dele:
            v.append(
                "MCPOAuthStore.kt: `signOut` deletes the bridge file BEFORE it "
                "imports the pending `.secret` handoff — the handoff file is "
                "gone by the time the import runs, so the client secret is "
                "lost (the import must happen first)"
            )

    # --- C6: purge must not resurrect the secret ---------------------------
    if "purge" in bodies:
        if _imports_pending_secret(code, "purge"):
            v.append(
                "MCPOAuthStore.kt: `purge` imports the pending `.secret` "
                "handoff — deleting a server must REMOVE its credentials, not "
                "write them into the encrypted store. The import would leave "
                "an orphan secret for a server the user just removed"
            )
    return v


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


def bridge_readers(code):
    """{name: body} of every function that reads a FILE under the bridge dir.

    Derived, not listed: a body that calls `readText(` on something the bridge
    path helpers built. The prefs reader (`tokens(`) pulls a string out of
    EncryptedSharedPreferences and therefore never qualifies — which is what
    keeps the closure in C1 from being trivially satisfied.
    """
    out = {}
    for name, body, _line in functions(code):
        if body is None or name == PREFS_TOKEN_READER:
            continue
        # A WRITER is not a reader: `materializeBridgeFile` contains
        # `readText(tmp)` in its rename fallback (it copies the half-written
        # temp file), and counting that as "the bridge is read back" would make
        # this gate pass on the pre-fix tree — the exact shape it exists to
        # catch.
        if BRIDGE_WRITE_CALL in body:
            continue
        if BRIDGE_READ_CALL in body and any(m in body for m in BRIDGE_PATH_MARKERS):
            out[name] = body
    return out

def call_closure(code, starts):
    """Names reachable from [starts] through calls to functions declared here."""
    bodies = {n: b for n, b, _l in functions(code) if b is not None}
    seen = set()
    stack = [n for n in starts if n in bodies]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for m in CALL_RE.finditer(bodies[name]):
            callee = m.group(1)
            if callee in bodies and callee not in seen:
                stack.append(callee)
    return seen

def _secret_handoff_readers(code_scoped):
    """Names of functions that read the `.secret` HANDOFF file.

    The `.secret` file carries the client secret the in-guest CLI could not
    put into the encrypted store; the token bridge is the sibling `<id>.json`.
    Both live under the bridge dir and both are read with `readText(`, so the
    structural view cannot tell them apart — the string-preserving view can.
    Without this separation the handoff reader looks like a token-bridge
    reader, and the promotion criteria (C1-C3) would judge the WRONG file.
    """
    out = set()
    for name, body, _line in functions(code_scoped):
        if body is None:
            continue
        if ".secret" in body:
            out.add(name)
    return out


def check_bridge_promotion(store, store_scoped, merge):
    """[dv26-A2] C1-C3 — the guest's refreshed token must survive a config save.

    [store] is comment/string-masked (structure only); [store_scoped] keeps
    string literal CONTENTS (the bridge key names are the cross-language
    contract, so they are anchored); [merge] is the Android-free policy file
    that holds the decision.

    C1  the path that materializes the bridge must READ it first and PERSIST
        what it read. Two thresholds, both needed: reading without persisting
        only moves the regression one save later (the next save reads the
        superseded triple back), and persisting without reading is a no-op.
        Derived through the call graph, so a helper that hides the read still
        counts, and a read that is only mentioned in a comment does not.
    C2  the promotion must ORDER the bridge against the stored copy (an older
        bridge must never win: that is the shape of "the app authorized after
        the guest's last refresh") AND compare the tokens themselves (a
        same-second refresh has an identical expiry, so ordering alone drops
        it). This is not a ritual: each guard is one of the two ways the
        promotion silently stops working.
    C3  the bridge is a token carrier, not a configuration source: the reader
        must not touch `client_id` / `token_endpoint` / `client_secret` /
        `resource` (derived from what `materializeBridgeFile` writes, minus the
        token triple). A stale or hand-edited bridge able to redirect a refresh
        to another endpoint is a worse bug than the one this fixes.

    Deliberately NOT pinned here: whether the ordering comparison is `>=` or
    `>` at the boundary. That is behaviour, and `MCPOAuthBridgeMergeTest`
    drives the same-second case with literals.

    All are fail-closed: when the sources cannot be read, or the anchors moved,
    that is a violation rather than a silent pass.
    """
    v = []

    def red(criterion, message):
        v.append("[dv26-A2] %s %s" % (criterion, message))

    materializers = [(n, ln) for n, b, ln in functions(store)
                     if b is not None and MATERIALIZE_CALL in b]
    if not materializers:
        red("C1", "no function calls `%s` — the bridge is never written, so this "
                  "criterion has lost its subject (ROT: re-point this gate)"
            % MATERIALIZE_CALL)
        return v
    readers = bridge_readers(store)
    # Same walk on the string-preserving view: the bridge KEY NAMES (and the
    # constructor arguments they feed) are the cross-language contract, and the
    # masked view blanks exactly those literals.
    readers_scoped = bridge_readers(store_scoped)
    # [dv27-A3] The `.secret` HANDOFF file lives under the same bridge dir and
    # is read with the same `readText(`, so the structural walk sees it as a
    # token-bridge reader. It is not one: it carries the client secret, not the
    # token triple. Judging it under C1-C3 would read the wrong file (and made
    # the "no bridge reader" fixture report a spurious C2), so the handoff
    # reader is removed from the token-bridge set.
    handoff = _secret_handoff_readers(store_scoped)
    readers = {n: b for n, b in readers.items() if n not in handoff}
    readers_scoped = {n: b for n, b in readers_scoped.items() if n not in handoff}
    if not readers:
        red("C1", "no function reads the bridge file (`%s` under the bridge dir) "
                  "— the in-guest CLI's refreshed token can be overwritten by the "
                  "host's pre-refresh copy again, and a rotated refresh_token "
                  "comes back as invalid_grant" % BRIDGE_READ_CALL)
        return v

    bodies = {n: b for n, b, _l in functions(store) if b is not None}
    for name, line in materializers:
        reach = call_closure(store, [name])
        hit = reach & set(readers)
        if not hit:
            red("C1", "`%s` (line %d) materializes the bridge without reading it "
                      "through any helper — the guest's refreshed token is "
                      "overwritten by the stored snapshot on every config save"
                % (name, line))
            continue
        persisted = [f for f in reach
                     if "setTokens(" in bodies.get(f, "")
                     and (f in readers or call_closure(store, [f]) & set(readers))]
        if not persisted:
            red("C1", "`%s` (line %d) reads the bridge but never persists the "
                      "promotion (`setTokens(`) — the promotion is lost on the "
                      "next save, which reads the superseded triple back" % (name, line))

    # -- C3: the reader must not lift configuration out of the guest file ----
    payload_body = body_of(store_scoped, MATERIALIZE_CALL.rstrip("("))
    if payload_body is None:
        red("C3", "`%s` has no body in the store — the bridge payload contract "
                  "cannot be derived, so 'tokens travel guest -> host, endpoints "
                  "do not' cannot be checked (ROT: re-point this gate)"
            % MATERIALIZE_CALL.rstrip("("))
    else:
        keys = []
        for m in PUT_KEY_RE.finditer(payload_body):
            if m.group(1) not in keys:
                keys.append(m.group(1))
        missing = [k for k in BRIDGE_TOKEN_KEYS if k not in keys]
        auth_keys = [k for k in keys if k not in BRIDGE_TOKEN_KEYS]
        if missing:
            red("C3", "the bridge payload no longer carries %s — the token triple "
                      "is the only thing this contract moves in either direction "
                      "(ROT: update this gate)" % ", ".join(missing))
        if not auth_keys:
            red("C3", "the bridge payload carries no authorization field — the "
                      "host-authoritative half of the contract is gone (ROT)")
        for name, body in readers_scoped.items():
            for key in auth_keys:
                if '"%s"' % key in body:
                    red("C3", "`%s` reads '%s' from the bridge file — %s stay "
                              "authoritative on the host side (the saved config + "
                              "the encrypted secret); a stale or hand-edited bridge "
                              "must not be able to redirect a refresh"
                        % (name, key, "/".join(auth_keys)))

    # -- C2: the guards that make the promotion survive the boundary cases --
    merge_classes = set(DATA_CLASS_RE.findall(merge))
    bridge_types = set()
    for body in readers_scoped.values():
        for cls in merge_classes:
            if re.search(r"\b" + re.escape(cls) + r"\s*\(", body):
                bridge_types.add(cls)
    if not bridge_types:
        red("C2", "the bridge reader builds no snapshot type declared in %s — the "
                  "decision has no input type to check (ROT: re-point this gate)"
            % MERGE_REL)
        return v
    decision = None
    for m in re.finditer(r"\bfun\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", merge):
        for cls in bridge_types:
            pm = re.search(r"([A-Za-z_]\w*)\s*:\s*" + re.escape(cls) + r"\b", m.group(2))
            if pm:
                body = body_at(merge, m.start())
                if body:
                    decision = (m.group(1), pm.group(1), body)
                    break
        if decision:
            break
    if decision is None:
        red("C2", "no function in %s decides on a bridge snapshot — the promotion "
                  "rule and its guards are gone (ROT: re-point this gate)" % MERGE_REL)
        return v
    fname, param, body = decision
    # Field names are DERIVED from the reader (which key of the guest file maps
    # to which constructor argument), never spelled by this gate. Then a local
    # assigned from one of those fields is resolved back to its bridge field, so
    # `val a = bridge.accessToken.trim()` followed by `a != stored.accessToken`
    # still counts as "the access token is compared".
    derived = {}
    for key, pat in (
        ("access_token", re.compile(r"([A-Za-z_]\w*)\s*=\s*\w+\.optString\(\s*\"access_token\"")),
        ("refresh_token", re.compile(r"([A-Za-z_]\w*)\s*=\s*\w+\.optString\(\s*\"refresh_token\"")),
        ("expires_at", re.compile(r"([A-Za-z_]\w*)\s*=\s*\w+\.optLong\(\s*\"expires_at\"")),
    ):
        for rbody in readers_scoped.values():
            m = pat.search(rbody)
            if m:
                derived[key] = m.group(1)
                break
    if len(derived) != 3:
        red("C2", "cannot derive the token / expiry field names from the bridge "
                  "reader — the comparisons below have nothing to be checked "
                  "against (ROT: re-point this gate)")
        return v

    alias_fields = {}
    for nm, rhs in LOCAL_VAL_RE.findall(body):
        for key, field in derived.items():
            if re.search(r"\b" + re.escape(param) + r"\." + re.escape(field) + r"\b", rhs):
                alias_fields[nm] = key

    def bridge_field(expr):
        """The bridge field name this expression came from, or None."""
        head = expr.split(".")[0]
        if head == param:
            rest = expr.split(".")[1] if "." in expr else ""
            for key, field in derived.items():
                if rest == field:
                    return key
            return None
        return alias_fields.get(head)

    def other_side_is_not_bridge(expr):
        head = expr.split(".")[0]
        return head != param and head not in alias_fields

    ordering = False
    compared = set()
    for m in COMPARISON_RE.finditer(body):
        left, op, right = m.group(1), m.group(2), m.group(3)
        for a, b in ((left, right), (right, left)):
            field = bridge_field(a)
            if field is None or not other_side_is_not_bridge(b):
                continue
            if op in ORDERING_OPS:
                ordering = True
            elif op in INEQUALITY_OPS:
                compared.add(field)
    if not ordering:
        red("C2", "`%s` never orders the bridge expiry against the stored copy — a "
                  "bridge left over from BEFORE the host authorized would promote "
                  "the token it already replaced (a rollback, not a refresh)"
            % fname)
    if "access_token" not in compared:
        red("C2", "`%s` never compares the bridge access token against the stored "
                  "one — a same-second refresh has an identical expiry, so ordering "
                  "alone silently stops promoting it" % fname)
    if "refresh_token" not in compared:
        red("C2", "`%s` never compares the bridge refresh token against the stored "
                  "one — under refresh-token rotation the superseded token stays in "
                  "the store and the guest's next refresh is invalid_grant" % fname)
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
            # [dv28-a5 A-05] The call grew a third, named argument (the "Stage 7
            # already merged the server list" switch). The output is still the
            # SECOND positional/`dst`-named one, so tolerating exactly one
            # trailing `name = <expr>` keeps this criterion's teeth: the archive
            # that reaches the extraction is still pinned by name.
            m_strip = re.search(
                re.escape(OAUTH_STRIP_HELPER)
                + r"\s*\(\s*(?:[A-Za-z_]\w*\s*=\s*)?[\w.]+\s*,\s*"
                + r"(?:[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\s*"
                + r"(?:,\s*[A-Za-z_]\w*\s*=\s*[^,()]+\s*,?\s*)?\)",
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


# [dv28-a5 A-05] One file, two restore stages. Stage 7 merges the backup's
# server list into the live one (`MCPRepository.importJSON` keeps the rows the
# backup does not carry); Stage 9 then wrote `mcp/servers.json` out of the
# artifact archive verbatim, replacing the merged file with the backup's list.
# The target device's own servers disappeared from DISK — the in-memory merge
# hid it until the next cold start re-read servers.json.
STAGE7_MERGE_CALL = "mcpRepo.importJSON("
SERVER_LIST_ENTRY_LITERAL = "servers.json"
STAGE9_SKIP_DECL = "Stage 7 merge"
STANDALONE_RELOAD = "reloadFromDisk("
# Tokens that carry no decision — dropped before comparing the "which operands
# gate the merge" sets, so `mcpRepo != null` cannot be satisfied by `null`
# appearing on both sides.
KOTLIN_NOISE_TOKENS = frozenset((
    "true", "false", "null", "is", "as", "in", "if", "else", "return", "it",
    "this", "length", "size", "isEmpty", "isNotEmpty", "optJSONArray",
))


def _match_paren(code, lp):
    """Index of the `)` that closes the `(` at [lp], or -1."""
    depth = 0
    for j in range(lp, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _top_level_args(call_text):
    """Top-level arguments of a parenthesised call text, or []."""
    if not call_text.startswith("(") or not call_text.endswith(")"):
        return []
    inner = call_text[1:-1]
    args, depth, cur = [], 0, []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def _enclosing_guard_identifiers(body, pos):
    """Identifiers of the innermost `if (…)` whose braces span [pos].

    Derived, never enumerated: whatever expression the Stage 7 merge is gated
    on is exactly the set of operands the Stage 9 skip has to be derived from.
    "Enclosing" means the condition closes before [pos] AND the `{…}` block that
    follows it still contains [pos] — a later `if` whose condition happens to end
    after [pos] gates nothing here.
    """
    best = set()
    for m in re.finditer(r"\bif\s*\(", body):
        lp = body.find("(", m.start())
        if lp < 0 or lp > pos:
            continue
        rp = _match_paren(body, lp)
        if rp < 0 or rp > pos:
            continue
        brace = body.find("{", rp)
        if brace < 0:
            continue
        block = brace_block(body, brace)
        if block is None:
            continue
        if not rp < brace < pos < brace + len(block):
            continue
        inner = body[lp + 1:rp]
        best = set(re.findall(r"[A-Za-z_]\w*", inner)) - KOTLIN_NOISE_TOKENS
    return best


def check_stage9_premerged(scoped, masked):
    """[dv28-a5 A-05] The server list must not be written by two stages.

    Derived axes (no line numbers, no member lists):
      * Stage 7's merge call exists in `import` AND is gated by an `if (…)` —
        the identifiers of that guard are the operands Stage 9 has to consult.
      * the pre-extraction filter call carries a THIRD argument whose value is
        an identifier computed inside `import` from (at least two of) those
        same operands — `true`, or a flag that ignores `mcpRepo`, is not a
        decision: dropping unconditionally loses the list from an archive-only
        backup, copying unconditionally erases the merged one.
      * a predicate in the same file drops that ONE entry name (segments
        collapsed), and the filter consults it only behind that switch.
      * the `mcp` prefix must remain an extraction destination (the other MCP
        data files still restore — exclude the file, not the prefix).
      * C-ST9b, two legal shapes, whitelisted: either a re-import + standalone
        `reloadFromDisk()` after extraction, or a `skipped` literal naming the
        file and the stage that restored it.
    """
    v = []
    imp = body_of(masked, "import")
    imp_scoped = body_of(scoped, "import")
    if imp is None or imp_scoped is None:
        return v  # [da1034-A1] A2 already reports the missing body
    merge_pos = imp.find(STAGE7_MERGE_CALL)
    extract_pos = imp.find(OAUTH_EXTRACT_CALL)
    strip_pos = imp.find(OAUTH_STRIP_HELPER)
    if merge_pos < 0 or extract_pos < 0 or strip_pos < 0:
        # No merge stage (or no artifact stage, or A2's "never filtered"):
        # there is no second writer to reason about, and A2 owns those arms.
        return v

    args = _top_level_args(argument_text(imp, strip_pos))
    if len(args) < 3:
        v.append(
            "ConfigBackup.kt: `" + OAUTH_STRIP_HELPER + "(` does not learn that Stage 7 "
            "already merged this backup's server list — the archive's `"
            + SERVER_LIST_ENTRY_LITERAL + "` is still written verbatim over the merged "
            "file, so every server this device has and the backup does not is erased "
            "(on disk; invisible until the next cold start re-reads it)")
        return v

    m_named = re.match(r"([A-Za-z_]\w*)\s*=\s*([\s\S]+)$", args[2])
    expr = (m_named.group(2) if m_named else args[2]).strip()
    guard_ids = _enclosing_guard_identifiers(imp, merge_pos)
    if expr in ("true", "false") or not re.fullmatch(r"[A-Za-z_]\w*", expr):
        v.append(
            "ConfigBackup.kt: the Stage 9 server-list switch is passed as `" + expr
            + "` — a literal is not a decision. An unconditional drop loses the list "
            "from an archive-only backup (no `mcpServers` section: Stage 7 never ran); "
            "an unconditional copy erases the merged one. Derive it from the operands "
            "that gate the Stage 7 merge"
            + (" (" + ", ".join(sorted(guard_ids)) + ")" if guard_ids else ""))
    else:
        decls = re.findall(
            r"\b(?:val|var)\s+" + re.escape(expr)
            + r"\s*(?::\s*[A-Za-z_.]\w*\s*)?=\s*([^\n]+)",
            imp,
        )
        if not decls:
            v.append(
                "ConfigBackup.kt: `" + expr + "` is never computed inside `import` — the "
                "Stage 9 skip cannot be traced back to whether Stage 7 merged the list")
        else:
            ids = set(re.findall(r"[A-Za-z_]\w*", "\n".join(decls))) - KOTLIN_NOISE_TOKENS
            if len(ids & guard_ids) < 2:
                v.append(
                    "ConfigBackup.kt: `" + expr + "` is not derived from the operands that "
                    "gate the Stage 7 merge"
                    + (" (" + ", ".join(sorted(guard_ids)) + ")" if guard_ids else "")
                    + " — need at least two of them (both the repository and the backup "
                    "section decide whether a merge happened)")

    helper = body_of(scoped, OAUTH_STRIP_HELPER)
    sig = re.search(r"\bfun\s+" + re.escape(OAUTH_STRIP_HELPER) + r"\s*\(", scoped)
    params = []
    if sig is not None:
        params = re.findall(r"([A-Za-z_]\w*)\s*:", argument_text(scoped, sig.start()))
    predicates = [
        (name, body, line) for (name, body, line) in functions(scoped)
        if body and ('"' + SERVER_LIST_ENTRY_LITERAL + '"') in body and '"mcp"' in body
    ]
    if not predicates:
        v.append(
            "ConfigBackup.kt: no predicate pins `mcp/" + SERVER_LIST_ENTRY_LITERAL
            + "` — nothing drops the entry Stage 7 already merged, so the verbatim "
            "copy overwrites the merged list")
    elif helper is not None:
        pname = predicates[0][0]
        if len(params) < 3:
            v.append(
                "ConfigBackup.kt: `" + OAUTH_STRIP_HELPER + "` takes no switch for the "
                "pre-merged server list — the drop would run unconditionally and an "
                "archive-only backup (no `mcpServers` section) would lose its list")
        elif not re.search(
            re.escape(params[2]) + r"[\s\S]{0,200}?\b" + re.escape(pname) + r"\s*\(", helper
        ):
            v.append(
                "ConfigBackup.kt: `" + OAUTH_STRIP_HELPER + "` calls `" + pname + "(` "
                "without the `" + params[2] + "` switch in the same condition — the "
                "server-list entry is dropped unconditionally")

    if '"mcp"' not in imp_scoped:
        v.append(
            "ConfigBackup.kt: the `mcp` prefix is no longer an extraction destination — "
            "excluding the whole prefix is not the fix: every other MCP data file the "
            "agent wrote under mcp/ is lost too. Exclude the ONE file, not the prefix")

    if imp.find(STANDALONE_RELOAD, extract_pos) >= 0 and imp.find(
        STAGE7_MERGE_CALL, extract_pos
    ) >= 0:
        # Legal shape B: merge again from the restored file + re-read disk. The
        # reload/import must come AFTER the extraction — an earlier one (Stage
        # 5's `skillRepo.reloadFromDisk()`) says nothing about the artifact stage.
        if imp.find(STANDALONE_RELOAD, extract_pos) < imp.find(STAGE7_MERGE_CALL, extract_pos):
            v.append(
                "ConfigBackup.kt: the post-extraction `" + STANDALONE_RELOAD + "()` runs "
                "BEFORE the re-import — it would publish the pre-extraction list")
    else:
        literals = re.findall(r'"((?:[^"\\]|\\.)*)"', imp_scoped)
        if not any(
            SERVER_LIST_ENTRY_LITERAL in s and STAGE9_SKIP_DECL in s for s in literals
        ):
            v.append(
                "ConfigBackup.kt: the skipped server-list copy is not declared — either "
                "re-import after extraction and call `" + STANDALONE_RELOAD + "()`, or "
                "report it through `skipped` with a literal naming the file and the stage "
                "that restored it (e.g. \"artifacts: " + SERVER_LIST_ENTRY_LITERAL
                + " restored via " + STAGE9_SKIP_DECL + " (skipped raw copy)\")")
    return v


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
    # [dv27-A3] the `.secret` handoff (client secret the in-guest CLI could
    # not put into the encrypted store).
    violations += check_secret_handoff(store)
    # [dv26-A2] the guest -> host half of the token bridge (fail-closed: an
    # unreadable policy file is a violation, not a skipped check).
    try:
        store_scoped = mask_comments(read(root, STORE_REL))
        merge = mask(read(root, MERGE_REL))
    except OSError as exc:
        violations.append(
            "[dv26-A2] cannot read the bridge-merge sources (%s) — a token the "
            "in-guest CLI refreshed can be overwritten by the host's pre-refresh "
            "copy again" % exc)
    else:
        violations += check_bridge_promotion(store, store_scoped, merge)
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
        # [dv28-a5 A-05] the same artifact stage must not write the file Stage 7
        # merged (the server list) a second time.
        violations += check_stage9_premerged(mask_comments(backup_raw), mask(backup_raw))
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
        "on a credentials-free export; the server list Stage 7 merged is never "
        "re-copied over by the artifact stage; every CLI-written oauth key readable by "
        "fromJson with snake_case primary first and the disk format unchanged; "
        "a token the in-guest CLI refreshed is read back, promoted and persisted "
        "before the bridge is re-materialized%s)"
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

    fun clientSecret(context: Context, server: String): String? {
        importPendingClientSecret(context, server)?.let { return it }
        return prefs(context).getString(secretKey(server), null)?.takeIf { it.isNotEmpty() }
    }

    fun importPendingClientSecret(context: Context, server: String): String? {
        val file = File(bridgeDir(context), "$server.secret")
        if (!file.exists()) return null
        val secret = runCatching { file.readText().trim() }.getOrNull()
        if (secret.isNullOrEmpty()) return null
        setClientSecret(context, server, secret)
        runCatching { file.delete() }
        return secret
    }

    fun signOut(context: Context, server: String) {
        importPendingClientSecret(context, server)
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
                val t = promoteFromBridge(context, server, requireNotNull(stored))
                materializeBridgeFile(context, server, BridgePayload(accessToken = t.accessToken, tokenEndpoint = cfg.tokenEndpoint))
            }
        }
    }

    private fun readBridgeTokenSnapshot(context: Context, server: String): BridgeTokenSnapshot? {
        return runCatching {
            val file = bridgeFile(context, server)
            if (!file.exists()) return@runCatching null
            val o = JSONObject(file.readText())
            BridgeTokenSnapshot(
                accessToken = o.optString("access_token", ""),
                refreshToken = o.optString("refresh_token", "").ifBlank { null },
                expiresAtSeconds = o.optLong("expires_at", 0L),
            )
        }.getOrNull()
    }

    private fun promoteFromBridge(context: Context, server: String, stored: StoredTokens): StoredTokens {
        val bridge = readBridgeTokenSnapshot(context, server) ?: return stored
        val promoted = promoteTokensFromBridge(
            StoredTokenSnapshot(stored.accessToken, stored.refreshToken, stored.expiresAtMs), bridge,
        ) ?: return stored
        val tokens = StoredTokens(promoted.accessToken, promoted.refreshToken, promoted.expiresAtMs)
        setTokens(context, server, tokens)
        return tokens
    }

    fun materializeBridgeFile(context: Context, server: String, payload: BridgePayload) {
        val json = JSONObject().apply {
            put("access_token", payload.accessToken)
            payload.refreshToken?.let { put("refresh_token", it) }
            put("expires_at", payload.expiresAtSeconds)
            put("token_endpoint", payload.tokenEndpoint)
            put("client_id", payload.clientId)
            payload.clientSecret?.let { put("client_secret", it) }
        }
    }

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
        importPendingClientSecret(context, server)
        prefs(context).edit().remove(tokensKey(server)).apply()
        deleteBridgeFile(context, server)
    }""",
    """    fun signOut(context: Context, server: String) {
        importPendingClientSecret(context, server)
        prefs(context).edit().remove(tokensKey(server)).apply()
    }""",
)

# ── [dv27-A3] `.secret` handoff arms ──────────────────────────────────────
# C5a: the single exit point no longer imports the pending handoff — the
# reader falls back to the stale stored value and the token exchange omits
# `client_secret` (a confidential client answers 401).
BAD_STORE_NO_SECRET_IMPORT = GOOD_STORE.replace(
    """    fun clientSecret(context: Context, server: String): String? {
        importPendingClientSecret(context, server)?.let { return it }
        return prefs(context).getString(secretKey(server), null)?.takeIf { it.isNotEmpty() }
    }""",
    """    fun clientSecret(context: Context, server: String): String? =
        prefs(context).getString(secretKey(server), null)?.takeIf { it.isNotEmpty() }""",
)

# C5b: signOut deletes the bridge file BEFORE importing — the handoff file is
# gone by the time the import runs, so the client secret is lost.
BAD_STORE_SIGNOUT_ORDER = GOOD_STORE.replace(
    """    fun signOut(context: Context, server: String) {
        importPendingClientSecret(context, server)
        prefs(context).edit().remove(tokensKey(server)).apply()
        deleteBridgeFile(context, server)
    }""",
    """    fun signOut(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).apply()
        deleteBridgeFile(context, server)
        importPendingClientSecret(context, server)
    }""",
)

# C6: purge imports the handoff — deleting a server would write its secret
# back into the encrypted store, resurrecting an orphan credential.
BAD_STORE_PURGE_IMPORTS = GOOD_STORE.replace(
    """    fun purge(context: Context, server: String) {
        prefs(context).edit().remove(tokensKey(server)).remove(secretKey(server)).apply()
        deleteBridgeFile(context, server)
    }""",
    """    fun purge(context: Context, server: String) {
        importPendingClientSecret(context, server)
        prefs(context).edit().remove(tokensKey(server)).remove(secretKey(server)).apply()
        deleteBridgeFile(context, server)
    }""",
)

# reconcile degenerated into purge-only (crack 2 re-opens)
BAD_STORE_NO_MATERIALIZE = GOOD_STORE.replace(
    "                materializeBridgeFile(context, server, BridgePayload(accessToken = t.accessToken, tokenEndpoint = cfg.tokenEndpoint))\n",
    "",
)

# ── [dv26-A2] guest → host token promotion fixtures ───────────────────────
# The merge decision (Android-free policy file). Two guards carry the rule: an
# expiry ORDERING against the stored copy, and a token INEQUALITY (a same-second
# refresh has an identical expiry), plus the refresh-token rotation half.
GOOD_MERGE = """
internal data class BridgeTokenSnapshot(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtSeconds: Long,
)

internal data class StoredTokenSnapshot(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtMs: Long,
)

internal data class TokenPromotion(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtMs: Long,
)

internal fun promoteTokensFromBridge(
    stored: StoredTokenSnapshot,
    bridge: BridgeTokenSnapshot?,
): TokenPromotion? {
    if (bridge == null) return null
    val bridgeAccess = bridge.accessToken.trim()
    if (bridgeAccess.isEmpty()) return null
    val bridgeRefresh = bridge.refreshToken?.trim()?.takeIf { it.isNotBlank() }
    val bridgeSeconds = if (bridge.expiresAtSeconds > 0) bridge.expiresAtSeconds else 0L
    val storedSeconds = if (stored.expiresAtMs > 0) stored.expiresAtMs / 1000 else 0L
    if (bridgeSeconds < storedSeconds) return null
    val replaced = bridgeAccess != stored.accessToken
    val rotated = bridgeRefresh != null && bridgeRefresh != stored.refreshToken
    if (!replaced && !rotated) return null
    return TokenPromotion(
        accessToken = bridgeAccess,
        refreshToken = bridgeRefresh ?: stored.refreshToken,
        expiresAtMs = bridgeSeconds * 1000,
    )
}
"""

# the pre-fix shape: the promotion is there but never orders against the host
# copy — an OLDER bridge would overwrite the token the app just authorized with
BAD_MERGE_NO_ORDERING = GOOD_MERGE.replace(
    "    if (bridgeSeconds < storedSeconds) return null\n",
    "",
)

# the same-second refresh: the ordering guard stays, the refresh comparison goes
BAD_MERGE_NO_REFRESH_CMP = GOOD_MERGE.replace(
    "    val rotated = bridgeRefresh != null && bridgeRefresh != stored.refreshToken\n",
    "    val rotated = false\n",
)

# ... and the same for the access-token comparison
BAD_MERGE_NO_ACCESS_CMP = GOOD_MERGE.replace(
    "    val replaced = bridgeAccess != stored.accessToken\n",
    "    val replaced = false\n",
)

# the baseline shape: reconcile materializes the prefs copy straight away
BAD_STORE_NO_PROMOTION = GOOD_STORE.replace(
    "                val t = promoteFromBridge(context, server, requireNotNull(stored))\n"
    "                materializeBridgeFile(context, server, BridgePayload(accessToken = t.accessToken, tokenEndpoint = cfg.tokenEndpoint))",
    "                materializeBridgeFile(context, server, BridgePayload(accessToken = stored!!.accessToken, tokenEndpoint = cfg.tokenEndpoint))",
)

# read but never persisted: the next save reads the superseded triple back and
# loses the promotion, so the bridge keeps flapping
BAD_STORE_UNPERSISTED = GOOD_STORE.replace(
    "        setTokens(context, server, tokens)\n        return tokens",
    "        return tokens",
)

# the authority leak: the reader parses an endpoint out of the guest file
BAD_STORE_READS_ENDPOINT = GOOD_STORE.replace(
    '                expiresAtSeconds = o.optLong("expires_at", 0L),',
    '                expiresAtSeconds = o.optLong("expires_at", 0L),\n'
    '                tokenEndpoint = o.optString("token_endpoint", ""),',
)

# not one function reads the file: the bridge is write-only again
BAD_STORE_NO_FILE_READ = GOOD_STORE.replace(
    "            val o = JSONObject(file.readText())",
    "            val o = JSONObject()",
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
    suspend fun import(tmp: java.io.File, artifactRoots: List<java.io.File>?, skipped: ArrayList<String>, root: JSONObject, mcpRepo: MCPRepository?) {
        val mcpArr = root.optJSONArray("mcpServers")
        val serverListMergedInStage7 = mcpRepo != null && mcpArr != null && mcpArr.length() > 0
        if (mcpArr != null && mcpRepo != null) {
            mcpRepo.importJSON(mcpArr.toString())
        }
        val destByPrefix = HashMap<String, java.io.File>()
        for (root in artifactRoots!!) {
            val prefix = if (root.name == "mcp-servers") "mcp" else "shared"
            destByPrefix[prefix] = root
        }
        val filtered = java.io.File.createTempFile("artifact-restore-", ".zip")
        try {
            val filterResult = stripMcpOAuthEntries(
                tmp,
                filtered,
                dropPrestagedServerList = serverListMergedInStage7,
            )
            if (filterResult.oauthDropped > 0) {
                skipped.add("artifacts: skipped ${filterResult.oauthDropped} oauth bridge entries")
            }
            if (filterResult.prestagedDropped > 0) {
                skipped.add("artifacts: servers.json restored via Stage 7 merge (skipped raw copy)")
            }
            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)
        } finally {
            runCatching { filtered.delete() }
        }
    }

    internal fun stripMcpOAuthEntries(
        src: java.io.File,
        dst: java.io.File,
        dropPrestagedServerList: Boolean = false,
    ): Int {
        var dropped = 0
        if (isMcpOAuthEntryName(src.name)) dropped++
        if (dropPrestagedServerList && isMcpServerListEntryName(src.name)) dropped++
        return dropped
    }

    internal fun isMcpOAuthEntryName(name: String): Boolean {
        val segs = ArrayList<String>(4)
        for (seg in name.split('/')) {
            if (seg.isEmpty() || seg == ".") continue
            segs.add(seg)
        }
        return segs.size >= 2 && segs[0] == "mcp" && segs[1] == "oauth"
    }

    internal fun isMcpServerListEntryName(name: String): Boolean {
        val segs = ArrayList<String>(4)
        for (seg in name.split('/')) {
            if (seg.isEmpty() || seg == ".") continue
            segs.add(seg)
        }
        return segs.size == 2 && segs[0] == "mcp" && segs[1] == "servers.json"
    }
}
"""
WIRED = """            val filterResult = stripMcpOAuthEntries(
                tmp,
                filtered,
                dropPrestagedServerList = serverListMergedInStage7,
            )
            if (filterResult.oauthDropped > 0) {
                skipped.add("artifacts: skipped ${filterResult.oauthDropped} oauth bridge entries")
            }
            if (filterResult.prestagedDropped > 0) {
                skipped.add("artifacts: servers.json restored via Stage 7 merge (skipped raw copy)")
            }
            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)"""
BAD_BACKUP_NO_WIRING = GOOD_BACKUP.replace(WIRED, "            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(tmp, destByPrefix)")
BAD_BACKUP_AFTER_EXTRACT = GOOD_BACKUP.replace(
    WIRED,
    """            artifactFilesImported = ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)
            val filterResult = stripMcpOAuthEntries(
                tmp,
                filtered,
                dropPrestagedServerList = serverListMergedInStage7,
            )
            if (filterResult.oauthDropped > 0) {
                skipped.add("artifacts: skipped ${filterResult.oauthDropped} oauth bridge entries")
            }
            if (filterResult.prestagedDropped > 0) {
                skipped.add("artifacts: servers.json restored via Stage 7 merge (skipped raw copy)")
            }""")
BAD_BACKUP_HOLLOW_PREDICATE = GOOD_BACKUP.replace(
    'segs[0] == "mcp" && segs[1] == "oauth"', 'segs[0] == "mcp" && segs[1] == "nope"')
BAD_BACKUP_NO_REPORT = GOOD_BACKUP.replace(
    '            if (filterResult.oauthDropped > 0) {\n'
    '                skipped.add("artifacts: skipped ${filterResult.oauthDropped} oauth bridge entries")\n'
    '            }\n', "")
# [da1034-A1] 变异 m2 的形状：filter 仍在、顺序也对，但抽取吃的是原始 tmp。
BAD_BACKUP_EXTRACT_RAW = GOOD_BACKUP.replace(
    "ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)",
    "ArtifactArchiver.extractZipByPrefix(tmp, destByPrefix)")

# -- [dv28-a5 A-05] pre-merged server list fixtures --------------------------

STAGE9_PREDICATE_BRANCH = (
    "        if (dropPrestagedServerList && isMcpServerListEntryName(src.name)) dropped++\n")
# the drop is gone entirely (a restore overwrites what Stage 7 merged)
BAD_STAGE9_NO_DROP = GOOD_BACKUP.replace(STAGE9_PREDICATE_BRANCH, "")
# the skip is only DESCRIBED — a comment cannot satisfy a criterion
BAD_STAGE9_COMMENT_ONLY = GOOD_BACKUP.replace(
    STAGE9_PREDICATE_BRANCH,
    "        // mcp/servers.json is skipped here — Stage 7 already merged it\n")
# the switch is a literal: unconditional drop loses an archive-only backup's list
BAD_STAGE9_UNCONDITIONAL = GOOD_BACKUP.replace(
    "dropPrestagedServerList = serverListMergedInStage7,",
    "dropPrestagedServerList = true,")
# the flag exists but ignores whether a merge happened
BAD_STAGE9_NO_OPERAND = GOOD_BACKUP.replace(
    "val serverListMergedInStage7 = mcpRepo != null && mcpArr != null && mcpArr.length() > 0",
    "val serverListMergedInStage7 = true")
# the predicate pins a name that is not the server list
BAD_STAGE9_HOLLOW_PREDICATE = GOOD_BACKUP.replace(
    'segs.size == 2 && segs[0] == "mcp" && segs[1] == "servers.json"',
    'segs.size == 2 && segs[0] == "mcp" && segs[1] == "other.json"')
# "fix" by never routing the mcp prefix: every other MCP data file is lost too
BAD_STAGE9_WHOLE_PREFIX = GOOD_BACKUP.replace(
    'val prefix = if (root.name == "mcp-servers") "mcp" else "shared"',
    'val prefix = "shared"')
# nothing tells the user the entry was skipped
BAD_STAGE9_NO_DECL = GOOD_BACKUP.replace(
    '            if (filterResult.prestagedDropped > 0) {\n'
    '                skipped.add("artifacts: servers.json restored via Stage 7 merge (skipped raw copy)")\n'
    '            }\n', "")

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

    # [dv27-A3] the `.secret` handoff arms (C5/C6).
    expect_clean("compliant secret-handoff fixture",
                 check_secret_handoff(mask(GOOD_STORE)))

    h_noimport = check_secret_handoff(mask(BAD_STORE_NO_SECRET_IMPORT))
    expect_trip("clientSecret without the handoff import", h_noimport,
                "does not import the pending")

    h_order = check_secret_handoff(mask(BAD_STORE_SIGNOUT_ORDER))
    expect_trip("signOut importing after the bridge delete", h_order,
                "BEFORE it imports")

    h_purge = check_secret_handoff(mask(BAD_STORE_PURGE_IMPORTS))
    expect_trip("purge importing the handoff", h_purge,
                "imports the pending `.secret` handoff")

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

    # [dv28-a5 A-05] the same Stage 9 pass must not re-write what Stage 7 merged
    def stage9(fixture):
        return check_stage9_premerged(mask_comments(fixture), mask(fixture))

    expect_clean("compliant pre-merged server-list fixture", stage9(GOOD_BACKUP))

    s9_nodrop = stage9(BAD_STAGE9_NO_DROP)
    expect_trip("filter that keeps the pre-merged server list", s9_nodrop,
                "without the `dropPrestagedServerList` switch")

    s9_comment = stage9(BAD_STAGE9_COMMENT_ONLY)
    expect_trip("skip that is only a comment", s9_comment,
                "without the `dropPrestagedServerList` switch")

    s9_uncond = stage9(BAD_STAGE9_UNCONDITIONAL)
    expect_trip("unconditional server-list drop", s9_uncond,
                "a literal is not a decision")

    s9_noop = stage9(BAD_STAGE9_NO_OPERAND)
    expect_trip("flag that ignores the merge operands", s9_noop,
                "is not derived from the operands")

    s9_hollow = stage9(BAD_STAGE9_HOLLOW_PREDICATE)
    expect_trip("predicate pinning the wrong file", s9_hollow, "no predicate pins")

    s9_prefix = stage9(BAD_STAGE9_WHOLE_PREFIX)
    expect_trip("whole mcp prefix dropped", s9_prefix,
                "no longer an extraction destination")

    s9_decl = stage9(BAD_STAGE9_NO_DECL)
    expect_trip("undeclared server-list skip", s9_decl, "not declared")

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
    print("selftest dv28-a5 stage9 arms (no-drop/comment-only/unconditional/"
          "flag-ignores-guard/hollow-pred/whole-prefix/undeclared) -> "
          "%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (len(s9_nodrop), len(s9_comment), len(s9_uncond), len(s9_noop),
             len(s9_hollow), len(s9_prefix), len(s9_decl)))

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

    # [dv26-A2] bridge-promotion arms. Each fixture must trip EXACTLY one
    # criterion, so a green run proves all three have teeth and none of them is
    # a restatement of another (the C1 pair are its two thresholds: never
    # reading, and reading without persisting).
    def promo(store=GOOD_STORE, merge=GOOD_MERGE):
        # check_bridge_promotion wants the structural view AND the
        # string-literal-preserving view of the SAME source (the bridge key names
        # are the cross-language contract), so both come from mask_comments(store).
        return check_bridge_promotion(mask_comments(store), mask_comments(store), mask(merge))

    def expect_only_tag(name, res, tag):
        if not any(tag in item for item in res):
            failures.append("%s must trip %s (got %s)" % (name, tag, res))
            return
        for item in res:
            if tag not in item:
                failures.append("%s tripped an unexpected criterion: %s" % (name, item))

    good_promo = promo()
    expect_clean("compliant bridge-promotion fixture", good_promo)

    p_noread = promo(store=BAD_STORE_NO_PROMOTION)
    expect_only_tag("baseline materialize-without-read fixture", p_noread, "C1")

    p_nofile = promo(store=BAD_STORE_NO_FILE_READ)
    expect_only_tag("store that never reads the bridge file", p_nofile, "C1")

    p_nopersist = promo(store=BAD_STORE_UNPERSISTED)
    expect_only_tag("read-but-not-persisted fixture", p_nopersist, "C1")

    p_endpoint = promo(store=BAD_STORE_READS_ENDPOINT)
    expect_only_tag("reader lifting an endpoint fixture", p_endpoint, "C3")

    p_noorder = promo(merge=BAD_MERGE_NO_ORDERING)
    expect_only_tag("promotion without an ordering guard fixture", p_noorder, "C2")

    p_noacc = promo(merge=BAD_MERGE_NO_ACCESS_CMP)
    expect_only_tag("promotion without an access-token comparison fixture", p_noacc, "C2")

    p_norefresh = promo(merge=BAD_MERGE_NO_REFRESH_CMP)
    expect_only_tag("promotion without a refresh-token comparison fixture", p_norefresh, "C2")

    p_nomerge = promo(merge="")
    expect_only_tag("missing bridge-merge policy fixture", p_nomerge, "C2")

    print("selftest dv26-A2 bridge-promotion arms (no-read/no-file-read/"
          "not-persisted/reader-endpoint/no-ordering/no-access-cmp/"
          "no-refresh-cmp/no-policy) -> "
          "%d/%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (len(p_noread), len(p_nofile), len(p_nopersist), len(p_endpoint),
             len(p_noorder), len(p_noacc), len(p_norefresh), len(p_nomerge)))


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
