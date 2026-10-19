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
  U2  every `var … by remember` in `MCPFormTab` whose initializer mentions
      `editServer` must be keyed on that entry, so a changed entry re-seeds
      the field states instead of leaving the old values on screen (1027-E
      ModelGroupDetailScreen / 1028-C EnvVarFormSheet are the same defect).

U2 is enumerated, not counted by hand: a hand-written "16 fields" list is
exactly what drifts. The check walks every `remember` in the function body and
reports the un-keyed ones by line. Keyed-on-the-entry is required rather than
any key: `remember(nonce)` would satisfy "has a key" while still never
re-seeding.

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
    if not tail.startswith("?:"):
        v.append(
            "MCPIntegrationsScreen.kt: the live-entry lookup has no `?: ` "
            "fallback — an entry deleted while the sheet is open would pass "
            "null to %s, silently turning the edit sheet into the add flow"
            % UI_RESOLVE_PARAM
        )
    if "%s%s," % (UI_LIVE_PASS, live_name) not in code:
        v.append(
            "MCPIntegrationsScreen.kt: `%s` is resolved but never passed as "
            "`%s%s` — the sheet still receives the snapshot"
            % (live_name, UI_LIVE_PASS, live_name)
        )
    return v, enforced


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
        "all wired; no secret-only cleanup path; edit sheet edits the live entry%s)"
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

# resolved without the elvis fallback (deleted entry ⇒ add flow)
BAD_UI_NO_FALLBACK = GOOD_UI.replace(
    "servers.find { it.id == captured.id } ?: captured",
    "servers.find { it.id == captured.id }",
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
