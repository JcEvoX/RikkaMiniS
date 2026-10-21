#!/usr/bin/env python3
"""[fix/da1025-D] browser_tabs_lifecycle_check

ISSUE-04 gate: the browser tab-pool saved-state files
(filesDir/browser_tabs/<sid>.json — a session's full visited-URL history)
must live and die with the session, same as the bind-mounted session dir and
its media. Before this gate the file was outside both cleanup paths:
  - deleteSessionFiles never deleted it → deleted session's URL history
    stayed on disk forever;
  - reclaimOrphans never scanned it → invisible to the storage page's
    orphan sweep.

Checked in SessionFileStore.kt (code anchors only — line AND block comments
are stripped before judgment, so a KDoc that merely *mentions* browser_tabs
cannot satisfy any criterion):
  C1  `browser_tabs` root + `fun browserTabFile(` definition exist
      (fail-closed: missing anchor = red, never a silent skip);
  C2  `deleteSessionFiles` function body references `browserTabFile(`
      (the delete path actually removes the tab state file);
  C3  `reclaimOrphans` function body calls the orphan cleanup
      (`deleteOrphanBrowserTabFiles(` or equivalent anchor), and the
      orphan browser-tabs cleanup itself is session-shape guarded
      (`looksLikeSessionId` OR `__new__` prefix branch present).

[fix/dv14-B2] C4 — the pool's own teardown ordering, checked in
BrowserTabPool.kt (ONLY the pool file; the store criteria above are
unaffected):
  C4a `dispose()` must NOT call `saveState(` (or re-persist to disk): the
      unconditional saveState() in dispose resurrected a just-deleted
      session's browser_tabs/<sid>.json on disk;
  C4b dispose() must still destroy tabs (release half must survive);
  C4c onTrimMemory / evictIdleTabs must assign the evicted tab's URL into
      savedURLs AFTER `destroyTab(` — destroyTab removes the entry, so an
      assignment made before it was silently wiped and the URL was lost.
  Fail-closed: missing dispose/trim/evict bodies, a missing destroyTab call
  or a missing savedURLs assignment = red, never a silent skip.

[fix/dv16-B7] C4d — window.open / window.close must persist too. The pool
persists on every tab mutation EXCEPT `handleNewWindow` / `handleCloseWindow`
(the only two paths that changed `_tabs` / `_selectedTabId` without calling
saveState()): a JS window.open tab (OAuth popups are the largest traffic) or
a window.close()'d tab resurrected from / vanished out of
browser_tabs/<sid>.json on session reload, and selectedTabId could point at
a destroyed tab. Both bodies must carry `saveState(` (fail-closed: an
unextractable body = red, never a silent skip).

[fix/dv17-1 B-01/B-06] C5/C6 — the same pool file, two "written but never
consumed / never re-pointed" defects (also read from BrowserTabPool.kt):
  C5  a detected risk challenge must actually REACH the agent. The four
      structured fields on BrowserActionResult (riskChallengeDetected /
      riskChallengeKind / recommendedNextAction / throttleDelayMs) had zero
      readers in the tree — ChatToolExecutors consumes `result.text` alone —
      so the renderer must be called in the action-assembly path
      (`runAcquiredAction`) AND its product merged into the returned text
      (a `text =` write after the call). Judged on the CALL SHAPE
      (`BrowserRiskControl.challengeNoticeLine(`), never a bare name, and
      comments are stripped so a KDoc mention cannot satisfy it.
  C6  `setSession` must re-point download records onto the new session's
      workspace when the pool is rebound across a session boundary: the old
      id must be CAPTURED BEFORE `this.sessionId` is overwritten, the remap
      must be guarded by an actual id change, must go through the atomic
      `_downloads` update (never a plain `_downloads.value =` assignment,
      which races the download/URL writers), and `loadSavedState()` must stay
      the last step (fail-closed: unextractable bodies = red).

Usage: python3 browser_tabs_lifecycle_check.py <repo_root>
       python3 browser_tabs_lifecycle_check.py --root <repo_root>
       python3 browser_tabs_lifecycle_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import re
import sys

PKG = "src/android/app/src/main/java/com/rikkaminis/app/data/storage"
TARGET = "SessionFileStore.kt"
# [fix/dv14-B2] C4's scan target: ONLY the pool file (scope discipline —
# the store criteria C1-C3 read SessionFileStore.kt and stay untouched).
POOL_REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"
MARKER = "BROWSER_TABS_SELFTEST=PASS"


def strip_comments(src: str) -> str:
    """Remove // line comments AND /* */ block comments, string-aware."""
    out = []
    in_str = False
    in_line = False
    in_block = False
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
        if in_str:
            if c == "\\":
                out.append(src[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def slice_body(src: str, header: str) -> str:
    """Return the function body for `header` (e.g. 'fun deleteSessionFiles').

    Fail-closed: returns '' when the header is not found or braces never
    balance — callers treat '' as a violation, never a silent skip.
    """
    idx = src.find(header)
    if idx < 0:
        return ""
    open_idx = src.find("{", idx)
    if open_idx < 0:
        return ""
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == '"':
            # skip string literal (escape-aware) so braces inside don't count
            j = i + 1
            while j < len(src):
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            i = j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    return ""


def check_source(source: str) -> list:
    src = strip_comments(source)
    errs = []

    # C1: root + definition (fail-closed)
    if "browser_tabs" not in src:
        errs.append("C1: no browser_tabs root in SessionFileStore")
    if "fun browserTabFile(" not in src:
        errs.append("C1: no fun browserTabFile( definition found")

    # C2: delete path removes the tab state file
    del_body = slice_body(src, "fun deleteSessionFiles")
    if not del_body:
        errs.append("C2: deleteSessionFiles body not extractable (fail-closed)")
    elif "browserTabFile(" not in del_body:
        errs.append("C2: deleteSessionFiles does not delete the browser tab state file")

    # C3: orphan reclaim path covers browser_tabs
    reclaim_body = slice_body(src, "fun reclaimOrphans")
    if not reclaim_body:
        errs.append("C3: reclaimOrphans body not extractable (fail-closed)")
    elif "deleteOrphanBrowserTabFiles(" not in reclaim_body:
        errs.append("C3: reclaimOrphans does not reclaim orphan browser_tabs files")
    orphan_body = slice_body(src, "fun deleteOrphanBrowserTabFiles")
    if not orphan_body:
        errs.append("C3: deleteOrphanBrowserTabFiles not found (fail-closed)")
    else:
        if "looksLikeSessionId(" not in orphan_body:
            errs.append("C3: orphan browser_tabs cleanup not session-shape guarded")
        if "__new__" not in orphan_body:
            errs.append("C3: orphan browser_tabs cleanup misses the __new__ draft branch")

    return errs


# --- [fix/dv14-B2] C4: pool teardown ordering (separate function — the
# C1-C3 criteria read SessionFileStore.kt; C4 reads BrowserTabPool.kt only).

def check_pool_source(pool_src: str) -> list:
    """[fix/dv14-B2] C4 — dispose() must not re-persist; the evicted tab's
    URL must be stored AFTER destroyTab. Fail-closed throughout."""
    code = strip_comments(pool_src)
    errs = []
    disp = slice_body(code, "fun dispose")
    if not disp:
        errs.append("C4: fun dispose( body not extractable (fail-closed)")
    else:
        if "saveState(" in disp:
            errs.append("C4: dispose() must not call saveState( (deleted sessions were being resurrected)")
        if "destroyTab(" not in disp:
            errs.append("C4: dispose() no longer destroys tabs (fail-closed)")
    for fn in ("fun onTrimMemory", "fun evictIdleTabs"):
        body = slice_body(code, fn)
        label = fn[4:]
        if not body:
            errs.append(f"C4: {label} body not extractable (fail-closed)")
            continue
        dpos = body.find("destroyTab(")
        if dpos < 0:
            errs.append(f"C4: {label} does not call destroyTab( (fail-closed)")
            continue
        w = re.search(r"savedURLs\[[^\]]*\]\s*=", body)
        if not w:
            errs.append(f"C4: {label} never assigns the evicted tab's URL into savedURLs (fail-closed)")
        elif w.start() < dpos:
            errs.append(f"C4: {label} assigns savedURLs before destroyTab( — destroyTab's remove wipes the entry")

    # --- [fix/dv16-B7] C4d: window.open / window.close must persist too ---
    for fn in ("fun handleNewWindow", "fun handleCloseWindow"):
        body = slice_body(code, fn)
        label = fn[4:]
        if not body:
            errs.append(f"C4d: {label} body not extractable (fail-closed)")
            continue
        if "saveState(" not in body:
            errs.append(
                f"C4d: {label} changes tab state but never calls saveState( — "
                "window.open/close tabs resurrect from the stale state file "
                "(and selectedTabId can point at a destroyed tab)")

    # --- [fix/dv17-1 B-01] C5: a detected risk challenge must reach the agent.
    # Bound to the call shape (+ the text merge AFTER it), never a bare name:
    # the defect was exactly that the structured fields were written and never
    # read, and the only exit ChatToolExecutors reads is result.text.
    action = slice_body(code, "private suspend fun runAcquiredAction")
    if not action:
        errs.append("C5: runAcquiredAction body not extractable (fail-closed)")
    else:
        notice_pos = action.find("BrowserRiskControl.challengeNoticeLine(")
        if notice_pos < 0:
            errs.append(
                "C5: runAcquiredAction never renders the detected risk challenge "
                "(no BrowserRiskControl.challengeNoticeLine( call) — the agent "
                "sees the challenge page as undistinguished text and retries blindly")
        elif not re.search(r"\btext\s*=", action[notice_pos:]):
            errs.append(
                "C5: the rendered risk notice is never merged into the result text "
                "(no `text =` write after the challengeNoticeLine( call) — the "
                "notice is computed and dropped, so the agent still cannot read it")
        if "BrowserRiskControl." not in action:
            errs.append("C5: runAcquiredAction no longer consults BrowserRiskControl (fail-closed)")

    # --- [fix/dv17-1 B-06] C6: setSession must re-point download destinations.
    sess = slice_body(code, "fun setSession")
    if not sess:
        errs.append("C6: setSession body not extractable (fail-closed)")
    else:
        capture = re.search(r"val\s+\w+\s*=\s*this\.sessionId", sess)
        assign = re.search(r"this\.sessionId\s*=\s*sessionId", sess)
        if not capture:
            errs.append(
                "C6: setSession never captures the previous session id before rebinding "
                "(download records under the old workspace can no longer be found)")
        elif not assign:
            errs.append("C6: setSession no longer rebinds this.sessionId (fail-closed)")
        elif capture.start() > assign.start():
            errs.append(
                "C6: setSession overwrites this.sessionId BEFORE capturing the old id — "
                "the old session id is lost and no download path can be re-pointed")
        if not re.search(r"\w+\s*!=\s*sessionId", sess):
            errs.append(
                "C6: setSession re-points downloads without checking the session actually "
                "changed (a same-id / first bind must stay a byte-identical no-op)")
        if "_downloads.value =" in sess:
            errs.append("C6: setSession rewrites _downloads by plain assignment (use _downloads.update { })")
        if not (re.search(r"remap\w*Download\w*\s*\(", sess)
                or "updateDownloadEntry(" in sess
                or "_downloads.update {" in sess):
            errs.append(
                "C6: setSession never re-points download destinations onto the new session "
                "workspace — the draft-promotion path leaves every record pointing at the "
                "deleted __new__<uuid> directory and its open button dies silently")
    return errs


# --- [fix/dv14-B2] C4 fixtures (pool teardown ordering) ---
# Mirrors the production shapes at the decision points only: capture url →
# destroyTab → store; dispose releases without re-persisting.

POOL_COMPLIANT = """package com.rikkaminis.app.browser

class BrowserTabPool(context: Context) {
    private val savedURLs = mutableMapOf<Int, String>()
    private val _tabs = MutableStateFlow<List<Tab>>(emptyList())

    private fun destroyTab(tabs: MutableList<Tab>, tab: Tab) {
        tabs.remove(tab)
        // destroyTab removes the entry — storing before this call loses it.
        savedURLs.remove(tab.id)
        tab.manager.destroy()
    }

    fun dispose() {
        evictionJob?.cancel()
        evictionJob = null
        val current = _tabs.value
        if (current.isNotEmpty()) {
            val mutable = current.toMutableList()
            for (tab in current) {
                destroyTab(mutable, tab)
            }
            _tabs.value = emptyList()
        }
    }

    override fun onTrimMemory(level: Int) {
        val victims = _tabs.value.filter { isLongIdle(it) }
        val currentTabs = _tabs.value.toMutableList()
        for (tab in victims) {
            val url = tab.manager.currentURL.value
            destroyTab(currentTabs, tab)
            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }
        }
        _tabs.value = currentTabs
        saveState()
    }

    fun evictIdleTabs() {
        val currentTabs = _tabs.value.toMutableList()
        val toRemove = currentTabs.filter { !it.inUse }
        for (tab in toRemove) {
            val url = tab.manager.currentURL.value
            destroyTab(currentTabs, tab)
            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }
        }
        if (toRemove.isNotEmpty()) {
            _tabs.value = currentTabs
            saveState()
        }
    }

    // [fix/dv16-B7] C4d: window.open / window.close persist like every other
    // mutation path (mirrors the production shapes at the decision points).
    private fun handleNewWindow(resultMsg: Message) {
        val currentTabs = _tabs.value.toMutableList()
        if (currentTabs.size >= MAX_TABS) {
            return
        }
        val id = nextTabId++
        currentTabs.add(Tab(id = id, manager = manager))
        _tabs.value = currentTabs
        _selectedTabId.value = id
        saveState()
        resultMsg.sendToTarget()
    }

    private fun handleCloseWindow(manager: BrowserUseManager) {
        val currentTabs = _tabs.value.toMutableList()
        val idx = currentTabs.indexOfFirst { it.manager === manager }
        if (idx >= 0) {
            val closedId = currentTabs[idx].id
            destroyTab(currentTabs, currentTabs[idx])
            _tabs.value = currentTabs
            if (_selectedTabId.value == closedId && currentTabs.isNotEmpty()) {
                _selectedTabId.value = currentTabs.first().id
            }
            saveState()
        }
    }

    // [fix/dv17-1 B-01] C5: the challenge must be rendered AND merged into the
    // result text (mirrors the production shapes at the decision points).
    private suspend fun runAcquiredAction(input: String): String {
        val result = execute(input)
        val riskChallenge = detectChallenge(input)
        val stamped = stampTabId(result, 0)
        if (riskChallenge != null) {
            val notice = BrowserRiskControl.challengeNoticeLine(
                kind = riskChallenge.kind,
                recommendedNextAction = riskChallenge.recommendedNextAction,
                throttleDelayMs = 0L,
            )
            val text = if (notice != null) {
                stamped.text + "\\n" + notice
            } else {
                stamped.text
            }
            return stamped.copy(text = text, riskChallengeDetected = true).text
        }
        return stamped.text
    }

    // [fix/dv17-1 B-06] C6: capture the old id BEFORE rebinding, guard on an
    // actual change, re-point through the atomic update; loadSavedState() last.
    fun setSession(sessionId: String) {
        val previous = this.sessionId
        this.sessionId = sessionId
        if (previous != null && previous != sessionId) {
            _downloads.update { current ->
                remapDownloadDestinations(
                    entries = current,
                    oldDir = File(filesDir, "minis-sessions/$previous/workspace"),
                    newDir = File(filesDir, "minis-sessions/$sessionId/workspace"),
                    pathOf = { entry -> entry.destination },
                    withPath = { entry, path -> entry.copy(destination = path) },
                )
            }
        }
        _tabs.value.forEach { it.manager.minisSessionId = sessionId }
        loadSavedState()
    }
}
"""

# C5: the notice renderer is called but its product never reaches the text —
# exactly the "written and dropped" defect this criterion exists for.
POOL_V_RISK_NOT_MERGED = POOL_COMPLIANT.replace(
    """            val text = if (notice != null) {
                stamped.text + "\\n" + notice
            } else {
                stamped.text
            }
            return stamped.copy(text = text, riskChallengeDetected = true).text""",
    "            return stamped.copy(riskChallengeDetected = true).text",
)

# C5: no renderer call at all (regression to the pre-fix shape).
POOL_V_RISK_NO_CALL = POOL_COMPLIANT.replace(
    """            val notice = BrowserRiskControl.challengeNoticeLine(
                kind = riskChallenge.kind,
                recommendedNextAction = riskChallenge.recommendedNextAction,
                throttleDelayMs = 0L,
            )
""", "")

# C6: baseline shape — no capture, no remap (the draft promotion defect).
POOL_V_SESSION_NO_REMAP = POOL_COMPLIANT.replace(
    """        val previous = this.sessionId
        this.sessionId = sessionId
        if (previous != null && previous != sessionId) {
            _downloads.update { current ->
                remapDownloadDestinations(
                    entries = current,
                    oldDir = File(filesDir, "minis-sessions/$previous/workspace"),
                    newDir = File(filesDir, "minis-sessions/$sessionId/workspace"),
                    pathOf = { entry -> entry.destination },
                    withPath = { entry, path -> entry.copy(destination = path) },
                )
            }
        }
""",
    "        this.sessionId = sessionId\n",
)

# C6: the remap exists but goes through a plain assignment (races the writers).
POOL_V_SESSION_PLAIN_ASSIGN = POOL_COMPLIANT.replace(
    "            _downloads.update { current ->\n",
    "            _downloads.value = _downloads.value\n            _downloads.update { current ->\n",
)

# C4a: the pre-fix dispose re-persisted the (possibly just-deleted) session.
POOL_V_DISPOSE_SAVE = POOL_COMPLIANT.replace(
    "            _tabs.value = emptyList()\n        }\n    }\n\n    override fun onTrimMemory",
    "            _tabs.value = emptyList()\n        }\n        saveState()\n    }\n\n    override fun onTrimMemory",
)

# C4c: URL stored BEFORE destroyTab — silently wiped by its remove().
POOL_V_TRIM_ORDER = POOL_COMPLIANT.replace(
    "        for (tab in victims) {\n"
    "            val url = tab.manager.currentURL.value\n"
    "            destroyTab(currentTabs, tab)\n"
    "            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }\n",
    "        for (tab in victims) {\n"
    "            val url = tab.manager.currentURL.value\n"
    "            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }\n"
    "            destroyTab(currentTabs, tab)\n",
)

# C4c: evict path forgets the evicted tab's URL entirely.
POOL_V_EVICT_NO_STORE = POOL_COMPLIANT.replace(
    "        for (tab in toRemove) {\n"
    "            val url = tab.manager.currentURL.value\n"
    "            destroyTab(currentTabs, tab)\n"
    "            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }\n"
    "        }\n",
    "        for (tab in toRemove) {\n"
    "            destroyTab(currentTabs, tab)\n"
    "        }\n",
)

# C4b: dispose keeps re-persisting nothing but stops releasing the tabs.
POOL_V_DISPOSE_NO_DESTROY = POOL_COMPLIANT.replace(
    "            for (tab in current) {\n                destroyTab(mutable, tab)\n            }\n",
    "            for (tab in current) {\n                Log.i(TAG, \"dispose(): destroyed tab ${tab.id}\")\n            }\n",
)

# [fix/dv16-B7] C4d: window.open forgets to persist the new tab state.
POOL_V_NEWWIN_NOSAVE = POOL_COMPLIANT.replace(
    "        _selectedTabId.value = id\n        saveState()\n        resultMsg.sendToTarget()",
    "        _selectedTabId.value = id\n        resultMsg.sendToTarget()",
)

# [fix/dv16-B7] C4d: window.close forgets to persist the teardown.
POOL_V_CLOSEWIN_NOSAVE = POOL_COMPLIANT.replace(
    "            if (_selectedTabId.value == closedId && currentTabs.isNotEmpty()) {\n"
    "                _selectedTabId.value = currentTabs.first().id\n"
    "            }\n"
    "            saveState()\n",
    "            if (_selectedTabId.value == closedId && currentTabs.isNotEmpty()) {\n"
    "                _selectedTabId.value = currentTabs.first().id\n"
    "            }\n",
)


def selftest() -> int:
    compliant = """package com.rikkaminis.app.data.storage

/**
 * Singular owner of the on-disk footprint attached to a chat session.
 * The doc mentions browser_tabs/<sid>.json and browserTabFile( freely —
 * block comments are stripped, so none of this satisfies any criterion.
 */
class SessionFileStore internal constructor(
    private val filesDirForTestingOrProd: File,
) {
    val sessionsRoot: File = File(filesDirForTestingOrProd, "minis-sessions")
    private val mediaRoot: File = File(filesDirForTestingOrProd, "media")
    private val browserTabsRoot: File = File(filesDirForTestingOrProd, "browser_tabs")

    fun sessionDir(sessionId: String): File = File(sessionsRoot, sessionId)
    fun browserTabFile(sessionId: String): File = File(browserTabsRoot, "$sessionId.json")

    // delete the tab file alongside the session dir (line comment mentions
    // browserTabFile( — stripped, must not satisfy C2 by itself)
    fun deleteSessionFiles(sessionId: String): DeleteResult {
        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId)) &&
            deleteRecursivelyChecked(browserTabFile(sessionId))
        val mediaDeleted = mediaSessionDirs(sessionId).fold(true) { acc, dir ->
            deleteRecursivelyChecked(dir) && acc
        }
        return DeleteResult(sessionDeleted, mediaDeleted)
    }

    fun reclaimOrphans(liveSessionIds: Set<String>): ReclaimReport {
        val report = scanOrphans(liveSessionIds)
        deleteOrphanMediaLeaves(liveSessionIds)
        deleteOrphanBrowserTabFiles(liveSessionIds)
        return report
    }

    private fun deleteOrphanBrowserTabFiles(liveSessionIds: Set<String>) {
        if (!browserTabsRoot.exists()) return
        browserTabsRoot.listFiles()?.forEach { file ->
            val stem = file.name.removeSuffix(".json")
            val isSessionShaped = looksLikeSessionId(stem) || stem.startsWith("__new__")
            if (isSessionShaped && stem !in liveSessionIds) file.delete()
        }
    }
}
"""
    # V1: delete path no longer removes the tab state file
    v1 = compliant.replace(
        "        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId)) &&\n"
        "            deleteRecursivelyChecked(browserTabFile(sessionId))",
        "        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId))",
    )
    # V2: orphan reclaim loses the browser_tabs half
    v2 = compliant.replace("        deleteOrphanBrowserTabFiles(liveSessionIds)\n", "")
    # V3: no browser_tabs at all (regression to the defect status quo)
    v3 = """package com.rikkaminis.app.data.storage

class SessionFileStore internal constructor(
    private val filesDirForTestingOrProd: File,
) {
    val sessionsRoot: File = File(filesDirForTestingOrProd, "minis-sessions")
    private val mediaRoot: File = File(filesDirForTestingOrProd, "media")

    fun sessionDir(sessionId: String): File = File(sessionsRoot, sessionId)

    fun deleteSessionFiles(sessionId: String): DeleteResult {
        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId))
        val mediaDeleted = mediaSessionDirs(sessionId).fold(true) { acc, dir ->
            deleteRecursivelyChecked(dir) && acc
        }
        return DeleteResult(sessionDeleted, mediaDeleted)
    }

    fun reclaimOrphans(liveSessionIds: Set<String>): ReclaimReport {
        val report = scanOrphans(liveSessionIds)
        deleteOrphanMediaLeaves(liveSessionIds)
        return report
    }
}
"""
    # V4: KDoc mentions browser_tabs + browserTabFile( but no code (comment trap)
    v4 = v3.replace(
        "class SessionFileStore",
        "/**\n"
        " * KDoc claims we handle browser_tabs/<sid>.json and call browserTabFile(\n"
        " * and deleteOrphanBrowserTabFiles( — pure documentation, no code.\n"
        " */\n"
        "class SessionFileStore",
    )
    # V5: orphan cleanup loses the __new__ draft branch
    v5 = compliant.replace(
        "            val isSessionShaped = looksLikeSessionId(stem) || stem.startsWith(\"__new__\")",
        "            val isSessionShaped = looksLikeSessionId(stem)",
    )

    arms = [
        ("compliant", compliant, []),
        ("V1 delete-path missing tab file", v1, ["C2"]),
        ("V2 orphan reclaim missing browser_tabs", v2, ["C3"]),
        ("V3 regression to defect status quo", v3, ["C1"]),
        ("V4 KDoc-only mention (comment trap)", v4, ["C1", "C2", "C3"]),
        ("V5 orphan cleanup misses __new__ branch", v5, ["C3"]),
    ]
    failures = 0
    for name, sample, expect in arms:
        errs = check_source(sample)
        if expect == [] and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != [] and not errs:
            print(f"  FAIL {name}: expected red, got green")
            failures += 1
        elif expect != [] and not any(err.startswith(c) for c in expect for err in errs):
            print(f"  FAIL {name}: expected {expect}, got {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {errs if errs else 'green'}")

    # --- [fix/dv14-B2] C4 arms: pool teardown ordering (own fixtures) ---
    pool_arms = [
        ("C4 compliant pool", POOL_COMPLIANT, []),
        ("C4a dispose re-persists saveState()", POOL_V_DISPOSE_SAVE, ["C4"]),
        ("C4c trim stores the URL before destroyTab", POOL_V_TRIM_ORDER, ["C4"]),
        ("C4c evict drops the URL assignment", POOL_V_EVICT_NO_STORE, ["C4"]),
        ("C4b dispose no longer destroys tabs", POOL_V_DISPOSE_NO_DESTROY, ["C4"]),
        ("C4d window.open forgets saveState", POOL_V_NEWWIN_NOSAVE, ["C4d"]),
        ("C4d window.close forgets saveState", POOL_V_CLOSEWIN_NOSAVE, ["C4d"]),
        ("C5 risk notice rendered but dropped", POOL_V_RISK_NOT_MERGED, ["C5"]),
        ("C5 risk notice never rendered", POOL_V_RISK_NO_CALL, ["C5"]),
        ("C6 setSession forgets to re-point downloads", POOL_V_SESSION_NO_REMAP, ["C6"]),
        ("C6 setSession rewrites _downloads by assignment", POOL_V_SESSION_PLAIN_ASSIGN, ["C6"]),
    ]
    for name, sample, expect in pool_arms:
        errs = check_pool_source(sample)
        if expect == [] and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != [] and not errs:
            print(f"  FAIL {name}: expected red, got green")
            failures += 1
        elif expect != [] and not any(err.startswith(c) for c in expect for err in errs):
            print(f"  FAIL {name}: expected {expect}, got {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {errs if errs else 'green'}")
    if failures:
        print(f"SELFTEST FAILED ({failures} arms)")
        return 1
    print(MARKER)
    # [fix/dv17-1 B-01/B-06] separate marker for the consumer/remap criteria —
    # MARKER above is pinned verbatim by test_scan.py and must not change.
    print("BROWSER_TABS_RISK_NOTICE_AND_REMAP_SELFTEST=PASS")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("usage: --root <repo_root>", file=sys.stderr)
            return 2
        root = args[i + 1]
    elif len(args) == 1:
        root = args[0]
    else:
        print("usage: <repo_root> | --root <repo_root> | --selftest", file=sys.stderr)
        return 2
    path = os.path.join(root, PKG, TARGET)
    try:
        src = open(path).read()
    except OSError as e:
        print(f"browser_tabs_lifecycle_check RED: cannot read {TARGET}: {e}")
        return 1
    errs = check_source(src)
    # [fix/dv14-B2] C4 — real tree must carry the pool file (fail-closed).
    pool_path = os.path.join(root, POOL_REL)
    try:
        pool_src = open(pool_path).read()
    except OSError as e:
        errs.append(f"C4: cannot read BrowserTabPool.kt: {e}")
    else:
        errs += check_pool_source(pool_src)
    if errs:
        print("browser_tabs_lifecycle_check RED:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("browser_tabs_lifecycle_check OK (criteria: C1-C3 store lifecycle + C4/C4d pool teardown + window.open/close persistence + C5 risk notice reaches the agent text + C6 setSession re-points downloads atomically)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
