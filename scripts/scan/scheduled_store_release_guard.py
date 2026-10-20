#!/usr/bin/env python3
"""
Guard: a persisted-task-set removal must release the runner's in-process
single-flight state — from OUTSIDE the store lock.

The regression this exists for (deep-audit-1016 B-7c, fixed 2026-10-16):
`ScheduledTasksStore.deleteTask` and `importDocument` cleared the persisted
`running` marker but never touched `ScheduledTaskRunner`'s in-process lock
(`runningTaskId` + `dispatched`). Deleting the task that was running right now
therefore left its id holding the single-flight gate until
`reapAbandonedDispatch`'s deadline+grace elapsed (~30 min for a default task),
during which every other due task degraded to `overlap` and never fired.

Two things have to hold, and both are silent when broken:

  1. PRESENCE — every function in `ScheduledTasksStore.kt` that drops tasks from
     the document must notify the runner. Deciding "drops tasks" textually:
     the body contains `filterNot { it.id ==` (drops by id) or
     `copy(running = null)` (the restore path).
  2. PLACEMENT — the notification must be a *top-level statement of that
     function*, never nested inside the store's `lock.withLock { … }` /
     `mutate(…) { … }` block. The notification takes the runner's `checkMutex`,
     which `checkAndFireToday` holds across a whole fire pass — and a fire pass
     calls back into the store (`markRunning`). Nesting the two locks inverts
     the order and deadlocks: the delete would wait for checkMutex while the
     fire pass waits for the store mutex.

Criterion (brace-depth based, no regex judgement calls): for each function
detected by rule 1, require exactly one `ScheduledTaskRunner.onTasksRemoved(`
call, at depth 1 of that function's body. Plus a file-wide rule: no
`ScheduledTaskRunner.` reference may appear while a `lock.withLock {` block is
open.

Usage: python3 scripts/scan/scheduled_store_release_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s) / ROT
"""
import os
import re
import sys

STORE_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/scheduled/ScheduledTasksStore.kt"
)

RELEASE_CALL = "ScheduledTaskRunner.onTasksRemoved("
FUN_RE = re.compile(r"^    (?:[a-z]+ )*fun (\w+) *\(", re.MULTILINE)
REMOVAL_RES = (
    re.compile(r"filterNot *\{ *it\.id *=="),
    re.compile(r"copy\(running *= *null\)"),
)
LOCK_RE = re.compile(r"\block\.withLock *\{")

# ── deep-audit-1030 ISSUE-08 ────────────────────────────────────────────────
# The editor used to save its WHOLE record, so any run that settled while the
# editor was open (up to the full 60 s prompt budget) was erased by the save —
# `ScheduledTask` carries run state (lastRunDate / lastResult / failStreak /
# suspendedByFuse) and `enabled` in the same record. The fix has two halves, and
# both are silent when broken: the store must route through the merge, and the
# merge must copy ONLY the editable spec fields.
#
# The subject of these criteria is "wherever an `upsertTask` exists" and "the
# edit screen", so a synthetic tree that carries neither reports SKIP instead of
# a violation (the real tree always carries both).
# ────────────────────────────────────────────────────────────────────────────

POLICY_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/scheduled/ScheduledTaskPolicy.kt"
)
EDIT_SCREEN_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ScheduledTaskEditScreen.kt"
)

MERGE_FUN = "fun mergeUpsertTask("
MERGE_CALL = "mergeUpsertTask("

# Run state + enablement: owned by the runner / store, never writable by a save.
RUN_STATE_FIELDS = ("enabled", "lastRunDate", "lastResult", "failStreak", "suspendedByFuse")
# The editable spec, i.e. what a save IS allowed to write. Every one must be
# copied: a `copy()` with no arguments would make Save silently do nothing.
# Keep in step with ScheduledTask's spec fields.
SPEC_FIELDS = (
    "title", "prompt", "windowStart", "windowEnd",
    "maxTurns", "deadlineSec", "maxEstimatedTokens",
)
# The pre-fix shape: the whole incoming record assigned into the list.
WHOLE_RECORD_ASSIGN = re.compile(r"(?:it|tasks)\[idx\]\s*=\s*task\b")
NAMED_ARG_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=(?!=)")

# ── deep-audit-1034 B-04 (S3) ───────────────────────────────────────────────
# The edit screen's two remaining silent states (reported 2026-10-20):
#   * record gone → the form kept its defaults with `original == null`: Save
#     could never enable (a dead button) while Delete stayed live on an id that
#     no longer exists. The screen said nothing about why.
#   * window inverted (start >= end, both halves parse) → the start field's
#     `isError = !startOk` left BOTH fields unmarked while `windowOk == false`
#     silently kept Save disabled.
# The criteria (S3) require: the id-keyed load raises `recordMissing` on the
# not-found path, the flag is a remembered `var`, canSave gates on it, an
# `if (recordMissing) { … return }` render gate keeps Save/Delete unreachable
# in the not-found screen, and the start field's isError binds the inverted
# window. The load spelling is an anchor — if it changes, ROT instead of
# passing vacuously.

# S2: the accepted spelling, pinned literally (whitespace-insensitive).
CAN_SAVE_RE = re.compile(r"val canSave\s*=")
CAN_SAVE_FORMS = ("(isNew||original!=null)", "(original!=null||isNew)")

# ── deep-audit-1034 B-04 ────────────────────────────────────────────────────
# Two more silent states of the edit screen (reported 2026-10-20):
#   * record gone → the form rendered its defaults with `original == null`, so
#     Save could never enable (a dead button) while Delete stayed live on an id
#     that no longer exists; the screen said nothing.
#   * window inverted (start >= end, both halves parse) → `isError = !startOk`
#     left BOTH fields unmarked while `windowOk == false` silently kept Save
#     disabled: a valid-looking window that never saves.
# The subject is the id-keyed load in ScheduledTaskEditScreen; the load spelling
# is an anchor — if it changes, ROT instead of passing vacuously.
# ────────────────────────────────────────────────────────────────────────────

MISSING_FLAG = "recordMissing"
MISSING_LOAD_RE = re.compile(r"firstOrNull *\{ *it\.id *== *taskId *\}")
MISSING_DECL_RE = re.compile(r"var +recordMissing +by +remember")
LOAD_EFFECT_HEAD = "LaunchedEffect(taskId)"
START_FIELD_ANCHOR = "R.string.scheduled_field_window_start"


def lambda_body(scan, head):
    """Body of the first trailing lambda attached to `head`, or None.

    `head` is e.g. `LaunchedEffect(taskId)`; the body is the `{ … }` that
    follows it, brace-matched. Input is strip_code output, so offsets line up
    with the raw file."""
    m = re.search(re.escape(head), scan)
    if m is None:
        return None
    j = m.end()
    while j < len(scan) and scan[j].isspace():
        j += 1
    if j >= len(scan) or scan[j] != "{":
        return None
    depth = 0
    for k in range(j, len(scan)):
        if scan[k] == "{":
            depth += 1
        elif scan[k] == "}":
            depth -= 1
            if depth == 0:
                return scan[j + 1:k]
    return None


def start_field_args(scan):
    """Args of the SectionTextField right after the `window_start` RowLabel, or
    None. The start field is where an inverted window must become visible."""
    m = re.search(re.escape(START_FIELD_ANCHOR), scan)
    if m is None:
        return None
    m2 = re.compile(r"SectionTextField *\(").search(scan, m.end())
    if m2 is None:
        return None
    i = m2.end() - 1
    depth = 0
    for k in range(i, len(scan)):
        if scan[k] == "(":
            depth += 1
        elif scan[k] == ")":
            depth -= 1
            if depth == 0:
                return scan[i + 1:k]
    return None


def scan_edit_screen_guards(screen_raw):
    """S3 — a gone record must not keep a live Delete on a dead id behind a Save
    that can never enable, and an inverted window must be visible on the form,
    not only as a silently disabled button."""
    scan = strip_code(screen_raw)
    bad = []

    # Subject anchor: the id-keyed load. If the spelling changed, ROT loudly.
    if not MISSING_LOAD_RE.search(scan):
        return [
            "%s: no `firstOrNull { it.id == taskId }` load left in ScheduledTaskEditScreen — "
            "the subject of the not-found criteria is gone (update this gate's detection)"
            % EDIT_SCREEN_REL
        ]

    # (1) the not-found branch must surface the condition, not just drop it.
    effect = lambda_body(scan, LOAD_EFFECT_HEAD)
    if effect is None:
        bad.append(
            "%s: no `%s { … }` body parseable — the load effect changed shape "
            "(update this gate's detection)" % (EDIT_SCREEN_REL, LOAD_EFFECT_HEAD)
        )
    elif "recordMissing=true" not in re.sub(r"\s+", "", effect):
        bad.append(
            "%s: the edit-target load no longer raises `%s` on the not-found path — a deleted "
            "record renders the form's defaults again: Save locked for good "
            "(`original == null`) while Delete stays live on an id that no longer exists"
            % (EDIT_SCREEN_REL, MISSING_FLAG)
        )

    # (2) the flag must be a remembered `var` — the render gate below reads it.
    if not MISSING_DECL_RE.search(scan):
        bad.append(
            "%s: no `var %s by remember` — the not-found state is not remembered, so the "
            "render gate cannot hold across recompositions" % (EDIT_SCREEN_REL, MISSING_FLAG)
        )

    # (3) Save must not depend on the not-found screen staying put: the flag
    # gates canSave directly (belt to `original != null`'s braces).
    m = CAN_SAVE_RE.search(scan)
    if m is None:
        bad.append(
            "%s: no `val canSave =` — the save gate is missing entirely" % EDIT_SCREEN_REL
        )
    else:
        statement = scan[m.end():].split("\n\n")[0]
        if "!" + MISSING_FLAG not in re.sub(r"\s+", "", statement):
            bad.append(
                "%s: canSave does not gate on `%s` — the not-found render path would be the "
                "only thing keeping Save unreachable, and rendering paths get refactored away"
                % (EDIT_SCREEN_REL, MISSING_FLAG)
            )

    # (4) the not-found state must be rendered as an explicit screen with one
    # way out — no Save/Delete shell in that branch.
    gate = None
    for gm in re.finditer(r"if *\(%s *\)" % MISSING_FLAG, scan):
        j = gm.end()
        while j < len(scan) and scan[j] != "{":
            if not scan[j].isspace():
                break
            j += 1
        if j < len(scan) and scan[j] == "{":
            depth = 0
            for k in range(j, len(scan)):
                if scan[k] == "{":
                    depth += 1
                elif scan[k] == "}":
                    depth -= 1
                    if depth == 0:
                        body = scan[j + 1:k]
                        if "return" in re.sub(r"\s+", "", body):
                            gate = body
                        break
        if gate is not None:
            break
    if gate is None:
        bad.append(
            "%s: no `if (%s) { … return }` render gate — the not-found state is set but never "
            "rendered, so the dead Save / live Delete form is back"
            % (EDIT_SCREEN_REL, MISSING_FLAG)
        )
    else:
        normalized_gate = re.sub(r"\s+", "", gate)
        for excluded in ("MinisButton(", "SectionTextField("):
            if excluded in normalized_gate:
                bad.append(
                    "%s: the not-found screen renders a `%s` — only the explanation and one "
                    "way out belong there; Save/Delete must stay unreachable when the record "
                    "is gone" % (EDIT_SCREEN_REL, excluded.rstrip("("))
                )

    # (5) an inverted window must be visible on the form, not only as a
    # disabled Save: the start field's isError has to bind more than its own
    # parse.
    args = start_field_args(scan)
    if args is None:
        bad.append(
            "%s: the `%s` field could not be located — the inverted-window visibility "
            "criterion is blind (update this gate's detection)"
            % (EDIT_SCREEN_REL, START_FIELD_ANCHOR)
        )
    else:
        em = re.search(r"isError\s*=", args)
        if em is None:
            bad.append(
                "%s: the start field has no isError binding — a window whose halves parse but "
                "invert (start >= end) is invisible on the form while Save silently stays "
                "disabled" % EDIT_SCREEN_REL
            )
        else:
            normalized_arg = re.sub(r"\s+", "", args[em.end():].split(",")[0])
            if "windowInverted" not in normalized_arg and "windowOk" not in normalized_arg:
                bad.append(
                    "%s: the start field's isError only flags its own parse (`%s`) — an "
                    "inverted window (both halves parse, start >= end) leaves both fields "
                    "unmarked while windowOk == false silently disables Save"
                    % (EDIT_SCREEN_REL, normalized_arg or "!startOk")
                )
    return bad



def copy_arg_names(body):
    """Union of the named arguments of every `copy(` call inside [body].

    Union rather than "the second call": the merge body holds the append
    (`file.copy(tasks = …)`), the merge itself (`existing.copy(…)`) and the
    write-back (`file.copy(tasks = …)`) — the criterion is about what a save can
    express, so every copy in the function is in scope.
    """
    names = set()
    for m in re.finditer(re.escape("copy("), body):
        open_at = m.end() - 1
        depth = 0
        for i in range(open_at, len(body)):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    names |= {g.group(1) for g in NAMED_ARG_RE.finditer(body[open_at + 1:i])}
                    break
    return names


def scan_merge_semantics(store_raw, merge_raw):
    """S1 — an editor save must not be able to speak about run state.

    None = not applicable (this tree has no `upsertTask`)."""
    scan = strip_code(store_raw)
    upsert = None
    for name, start, end, line in function_spans(scan):
        if name == "upsertTask":
            upsert = (scan[start:end], line)
    if upsert is None:
        return None
    body, line = upsert
    bad = []
    if MERGE_CALL not in body:
        bad.append(
            "%s:%d: [upsertTask] no longer routes through %s — a whole-record write lets a save "
            "erase any run that settled while the editor was open (the run state lives in the "
            "same record as the spec)" % (STORE_REL, line, MERGE_CALL.rstrip("("))
        )
    if WHOLE_RECORD_ASSIGN.search(body):
        bad.append(
            "%s:%d: [upsertTask] writes the caller's whole record (`it[idx] = task`) — run state "
            "and `enabled` come from the editor's stale snapshot that way"
            % (STORE_REL, line)
        )
    if merge_raw is None:
        if MERGE_CALL in body:
            bad.append(
                "%s:%d: [upsertTask] calls the merge but no `%s` exists under src/ — the function "
                "that keeps run state is gone (the tree cannot compile as-is)"
                % (STORE_REL, line, MERGE_FUN.strip())
            )
        return bad

    scan_merge = strip_code(merge_raw)
    merge_body = None
    for name, start, end, _line in function_spans(scan_merge):
        if name == "mergeUpsertTask":
            merge_body = scan_merge[start:end]
    if merge_body is None:
        bad.append("the merge function could not be parsed — the probe lost its subject")
        return bad
    args = copy_arg_names(merge_body)
    for field in RUN_STATE_FIELDS:
        if field in args:
            bad.append(
                "ScheduledTaskTransitions.mergeUpsertTask copies `%s` from the caller's record — "
                "that is the run state / enablement a save must never be able to overwrite "
                "(it belongs to the runner and the store)" % field
            )
    for field in SPEC_FIELDS:
        if field not in args:
            bad.append(
                "ScheduledTaskTransitions.mergeUpsertTask does not copy the editable field `%s` — "
                "an empty/short copy() makes Save silently drop that field (update this gate's "
                "SPEC_FIELDS when the editable spec changes)" % field
            )
    return bad


def scan_can_save_gate(screen_raw):
    """S2 — Save must wait for the stored record before it may write over it."""
    scan = strip_code(screen_raw)
    m = CAN_SAVE_RE.search(scan)
    if m is None:
        return [
            "ScheduledTaskEditScreen has no `val canSave =` — the save gate is missing entirely "
            "(the probe must not pass vacuously)"
        ]
    statement = scan[m.end():].split("\n\n")[0]
    normalized = re.sub(r"\s+", "", statement)
    if not any(form in normalized for form in CAN_SAVE_FORMS):
        return [
            "ScheduledTaskEditScreen.canSave does not gate on the loaded snapshot: expected "
            + " or ".join(CAN_SAVE_FORMS)
            + " — without it a save issued while LaunchedEffect(taskId) is still loading writes "
              "the form's defaults over the stored spec"
        ]
    return []


def find_merge_source(root):
    """(rel_path, text) of the file declaring `fun mergeUpsertTask(`, or (None, None)."""
    src = os.path.join(root, "src")
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            if MERGE_FUN in strip_code(text):
                return os.path.relpath(path, root), text
    return None, None


def read_optional(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def selftest():
    """Negative controls for the deep-audit-1030 criteria: each must reject its
    own mutated fixture and accept the clean one."""
    clean_store = (
        "object ScheduledTasksStore {\n"
        "    suspend fun upsertTask(context: Any, task: Any): Any =\n"
        "        mutate(context) { file -> ScheduledTaskTransitions.mergeUpsertTask(file, task) }\n"
        "}\n"
    )
    clean_merge = (
        "object ScheduledTaskTransitions {\n"
        "    fun mergeUpsertTask(file: ScheduledTasksFile, task: ScheduledTask): ScheduledTasksFile {\n"
        "        val idx = file.tasks.indexOfFirst { it.id == task.id }\n"
        "        if (idx < 0) return file.copy(tasks = file.tasks + task)\n"
        "        val existing = file.tasks[idx]\n"
        "        val merged = existing.copy(\n"
        "            title = task.title,\n"
        "            prompt = task.prompt,\n"
        "            windowStart = task.windowStart,\n"
        "            windowEnd = task.windowEnd,\n"
        "            maxTurns = task.maxTurns,\n"
        "            deadlineSec = task.deadlineSec,\n"
        "            maxEstimatedTokens = task.maxEstimatedTokens,\n"
        "        )\n"
        "        return file.copy(tasks = file.tasks.toMutableList().also { it[idx] = merged })\n"
        "    }\n"
        "}\n"
    )
    old_store = (
        "object ScheduledTasksStore {\n"
        "    suspend fun upsertTask(context: Any, task: Any): Any =\n"
        "        mutate(context) { file ->\n"
        "            val idx = file.tasks.indexOfFirst { it.id == task.id }\n"
        "            val tasks = if (idx < 0) {\n"
        "                file.tasks + task\n"
        "            } else {\n"
        "                file.tasks.toMutableList().also { it[idx] = task }\n"
        "            }\n"
        "            file.copy(tasks = tasks)\n"
        "        }\n"
        "}\n"
    )
    clean_screen = (
        "fun ScheduledTaskEditScreen(taskId: String?, onDone: () -> Unit) {\n"
        "    val isNew = taskId == null\n"
        "    var original by remember { mutableStateOf<ScheduledTask?>(null) }\n"
        "    val canSave = prompt.isNotBlank() && windowOk && turnsOk && deadlineOk && tokensOk &&\n"
        "        (isNew || original != null)\n"
        "\n"
        "    if (showDeleteConfirm) {\n"
        "    }\n"
        "}\n"
    )
    # deep-audit-1034 B-04: the not-found state + inverted-window visibility.
    # Built as prefix / render-gate / suffix so a mutation can drop the gate
    # wholesale without carrying a copy of it around.
    edit_prefix = (
        "fun ScheduledTaskEditScreen(taskId: String?, onDone: () -> Unit) {\n"
        "    val isNew = taskId == null\n"
        "    var original by remember { mutableStateOf<ScheduledTask?>(null) }\n"
        "    var recordMissing by remember { mutableStateOf(false) }\n"
        "\n"
        "    if (!isNew) {\n"
        "        LaunchedEffect(taskId) {\n"
        "            val loaded = file.tasks.firstOrNull { it.id == taskId }\n"
        "            if (loaded == null) {\n"
        "                recordMissing = true\n"
        "            } else {\n"
        "                original = loaded\n"
        "                recordMissing = false\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "\n"
        "    val startOk = parse(windowStart) != null\n"
        "    val endOk = parse(windowEnd) != null\n"
        "    val windowOk = startOk && endOk && start < end\n"
        "    val windowInverted = startOk && endOk && !windowOk\n"
        "    val canSave = !recordMissing && prompt.isNotBlank() && windowOk && turnsOk && deadlineOk &&\n"
        "        tokensOk && (isNew || original != null)\n"
        "\n"
    )
    edit_gate = (
        "    if (recordMissing) {\n"
        "        SettingsScaffold(\n"
        "            title = stringResource(R.string.scheduled_edit_title),\n"
        "            navigation = {\n"
        "                MinisTextButton(onClick = onDone) {\n"
        "                    Text(stringResource(R.string.common_back))\n"
        "                }\n"
        "            },\n"
        "        ) {\n"
        "            SettingsSection {\n"
        "                SettingsCardBlock {\n"
        "                    Text(text = stringResource(R.string.scheduled_edit_missing_body))\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        return\n"
        "    }\n"
        "\n"
    )
    edit_suffix = (
        "    SettingsSection {\n"
        "        RowLabel(text = stringResource(R.string.scheduled_field_window_start))\n"
        "        SectionTextField(\n"
        "            value = windowStart,\n"
        "            onValueChange = { windowStart = it },\n"
        "            isError = !startOk || windowInverted,\n"
        "            singleLine = true,\n"
        "        )\n"
        "    }\n"
        "}\n"
    )
    edit_screen = edit_prefix + edit_gate + edit_suffix

    cases = [
        ("store/clean", (clean_store, clean_merge), scan_merge_semantics),
        ("store/reverted to a whole-record write", (old_store, clean_merge), scan_merge_semantics),
        ("store/merge call dropped", (clean_store.replace(MERGE_CALL, "writeRecord("), clean_merge),
         scan_merge_semantics),
        ("merge/writes enabled back", (clean_store, clean_merge.replace(
            "            title = task.title,\n", "            title = task.title,\n            enabled = task.enabled,\n")),
         scan_merge_semantics),
        ("merge/writes lastRunDate back", (clean_store, clean_merge.replace(
            "            title = task.title,\n", "            title = task.title,\n            lastRunDate = task.lastRunDate,\n")),
         scan_merge_semantics),
        ("merge/copies nothing", (clean_store, clean_merge.replace(
            "            title = task.title,\n"
            "            prompt = task.prompt,\n"
            "            windowStart = task.windowStart,\n"
            "            windowEnd = task.windowEnd,\n"
            "            maxTurns = task.maxTurns,\n"
            "            deadlineSec = task.deadlineSec,\n"
            "            maxEstimatedTokens = task.maxEstimatedTokens,\n", "")),
         scan_merge_semantics),
        ("merge/function gone", (clean_store, None), scan_merge_semantics),
        ("screen/clean", clean_screen, scan_can_save_gate),
        ("screen/snapshot gate dropped", clean_screen.replace(
            "\n        (isNew || original != null)", ""), scan_can_save_gate),
        ("screen/no canSave at all", clean_screen.replace("val canSave", "val maySave"), scan_can_save_gate),
        ("editguard/clean", edit_screen, scan_edit_screen_guards),
        ("editguard/not-found branch dropped", edit_screen.replace(
            "                recordMissing = true\n", ""), scan_edit_screen_guards),
        ("editguard/flag not remembered", edit_screen.replace(
            "var recordMissing by remember", "var recordMissing ="), scan_edit_screen_guards),
        ("editguard/canSave guard dropped", edit_screen.replace(
            "!recordMissing && ", ""), scan_edit_screen_guards),
        ("editguard/not-found render removed", edit_prefix + edit_suffix, scan_edit_screen_guards),
        ("editguard/not-found screen keeps a save shell", edit_screen.replace(
            "                Text(text = stringResource(R.string.scheduled_edit_missing_body))\n",
            "                Text(text = stringResource(R.string.scheduled_edit_missing_body))\n"
            "                SectionTextField(value = prompt)\n"), scan_edit_screen_guards),
        ("editguard/start isError reverted to its own parse", edit_screen.replace(
            "isError = !startOk || windowInverted", "isError = !startOk"), scan_edit_screen_guards),
        ("editguard/subject load renamed", edit_screen.replace(
            "it.id == taskId", "it.id == otherId"), scan_edit_screen_guards),
    ]

    print("scheduled_store_release_guard --selftest")
    failures = 0
    merge_failures = 0
    cansave_failures = 0
    editguard_failures = 0
    for name, fixture, scan in cases:
        args = fixture if isinstance(fixture, tuple) else (fixture,)
        bad = scan(*args)
        ok = bool(bad) if not name.endswith("/clean") else (not bad)
        print(f"  {'✅' if ok else '❌'} {name} → {'clean' if not bad else '; '.join(bad)[:90]}")
        if not ok:
            failures += 1
            if name.startswith("merge/") or name.startswith("store/"):
                merge_failures += 1
            elif name.startswith("editguard/"):
                editguard_failures += 1
            else:
                cansave_failures += 1
    print(f"  {'✅ selftest passed' if not failures else f'❌ selftest failures: {failures}'}")
    print(f"SCHEDULED_MERGE_SELFTEST={'PASS' if merge_failures == 0 else 'FAIL'}")
    print(f"SCHEDULED_CANSAVE_SELFTEST={'PASS' if cansave_failures == 0 else 'FAIL'}")
    print(f"SCHEDULED_EDITGUARD_SELFTEST={'PASS' if editguard_failures == 0 else 'FAIL'}")
    return 1 if failures else 0


def strip_code(src):
    """Blank out comments and string literals, preserving offsets and newlines."""
    out = list(src)
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for _ in range(2):
                if i < n and src[i] != "\n":
                    out[i] = " "
                i += 1
        elif c == '"':
            if src[i:i + 3] == '"""':
                for k in range(3):
                    out[i + k] = " "
                i += 3
                while i < n and src[i:i + 3] != '"""':
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
                for _ in range(3):
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
            else:
                out[i] = " "
                i += 1
                while i < n and src[i] != '"':
                    if src[i] == "\\":
                        out[i] = " "
                        i += 1
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
                if i < n:
                    out[i] = " "
                    i += 1
        else:
            i += 1
    return "".join(out)


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def function_spans(scan):
    """[(name, body_start_index, body_end_index_exclusive, start_line)] for each fun."""
    spans = []
    for m in FUN_RE.finditer(scan):
        # The body opens at the first '{' after the signature.
        open_idx = scan.find("{", m.end())
        if open_idx < 0:
            continue
        depth = 0
        close_idx = None
        for j in range(open_idx, len(scan)):
            ch = scan[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    close_idx = j
                    break
        if close_idx is None:
            continue
        spans.append((m.group(1), open_idx, close_idx + 1, line_of(scan, m.start())))
    return spans


def depth_at(scan, start, idx):
    """Brace depth inside the function body at index `idx` (body brace = depth 1)."""
    return scan.count("{", start + 1, idx) - scan.count("}", start + 1, idx) + 1


def main():
    if "--selftest" in sys.argv:
        return selftest()

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, STORE_REL)
    findings = []
    rot = []

    if not os.path.isfile(path):
        print("ROT: %s not found — the gate cannot see the shape it guards." % STORE_REL)
        print("     If the store moved, teach this gate the new path.")
        return 1

    src = open(path, encoding="utf-8").read()
    scan = strip_code(src)
    spans = function_spans(scan)

    removal_fns = []
    for name, start, end, line in spans:
        body = scan[start:end]
        if any(r.search(body) for r in REMOVAL_RES):
            removal_fns.append((name, start, end, line))

    if not spans:
        rot.append("no `fun` at object-member indentation was parsed")
    if not removal_fns:
        rot.append(
            "no task-removal function was detected (expected deleteTask / "
            "importDocument shapes: `filterNot { it.id ==` or `copy(running = null)`)"
        )

    for name, start, end, line in removal_fns:
        body = scan[start:end]
        hits = [start + m.start() for m in re.finditer(re.escape(RELEASE_CALL), body)]
        if not hits:
            findings.append(
                "%s:%d: [%s] drops tasks from the persisted document but never calls "
                "%s — the runner's in-process single-flight lock survives the removal "
                "(B-7c)" % (STORE_REL, line, name, RELEASE_CALL.rstrip("("))
            )
            continue
        if len(hits) > 1:
            findings.append(
                "%s:%d: [%s] calls %s %d times — exactly one release per removal"
                % (STORE_REL, line, name, RELEASE_CALL.rstrip("("), len(hits))
            )
        for hit in hits:
            d = depth_at(scan, start, hit)
            if d != 1:
                findings.append(
                    "%s:%d: [%s] %s sits at brace depth %d (nested inside the store "
                    "lock) — LOCK ORDER: the release takes the runner's checkMutex, "
                    "which a fire pass holds while calling back into this store "
                    "(`markRunning`); nesting deadlocks. Call it after the lock."
                    % (STORE_REL, line_of(scan, hit), name, RELEASE_CALL.rstrip("("), d)
                )

    # File-wide: no runner reference while a store lock block is open.
    depth = 0
    lock_body_depth = None
    for lineno, raw in enumerate(scan.split("\n"), start=1):
        if lock_body_depth is None and LOCK_RE.search(raw):
            lock_body_depth = depth + 1
        if "ScheduledTaskRunner." in raw and lock_body_depth is not None:
            findings.append(
                "%s:%d: ScheduledTaskRunner referenced inside a `lock.withLock` block "
                "(opened at body depth %d) — LOCK ORDER (see "
                "ScheduledTaskRunner.onTasksRemoved)"
                % (STORE_REL, lineno, lock_body_depth)
            )
        depth += raw.count("{") - raw.count("}")
        if lock_body_depth is not None and depth < lock_body_depth:
            lock_body_depth = None

    # deep-audit-1030 ISSUE-08: an editor save must not be able to speak about
    # run state. These subjects live in companion files; on a synthetic tree
    # that carries neither, they report SKIP rather than a violation.
    merge_rel, merge_raw = find_merge_source(root)
    s1 = scan_merge_semantics(src, merge_raw)
    if s1 is None:
        print("SKIP: no `upsertTask` in %s — the merge criteria do not apply here" % STORE_REL)
    elif s1:
        findings.extend(s1)
    else:
        print("OK: ScheduledTasksStore.upsertTask routes through %s — a save copies the "
              "editable spec only; run state and `enabled` stay with the stored record"
              % (merge_rel or "the merge"))
    screen_raw = read_optional(root, EDIT_SCREEN_REL)
    if screen_raw is None:
        print("SKIP: %s not in this tree — the canSave / not-found criteria do not apply here"
              % EDIT_SCREEN_REL)
    else:
        s2 = scan_can_save_gate(screen_raw)
        if s2:
            findings.extend(s2)
        else:
            print("OK: ScheduledTaskEditScreen.canSave waits for the loaded snapshot "
                  "before an edit may be saved")
        s3 = scan_edit_screen_guards(screen_raw)
        if s3:
            findings.extend(s3)
        else:
            print("OK: ScheduledTaskEditScreen surfaces a gone record (explicit not-found "
                  "screen; Save and Delete unreachable there) and flags an inverted window "
                  "on the form rather than leaving Save silently disabled")

    if rot:
        print("ROT: %s" % STORE_REL)
        for r in rot:
            print("  - %s" % r)
        print("  The gate fails loudly instead of passing vacuously. If the store was")
        print("  refactored, update the detection in this script.")
        return 1

    if findings:
        for f in findings:
            print(f)
        print("\n%d violation(s)." % len(findings))
        return 1

    print(
        "OK: %d removal function(s) release the runner (%s) at top level."
        % (len(removal_fns), ", ".join(n for n, _, _, _ in removal_fns))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
