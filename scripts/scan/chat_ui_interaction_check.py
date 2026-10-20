#!/usr/bin/env python3
"""
Gate: FIX-4 (deep-audit-1037, dv16 wave) — chat UI interaction wiring. Three
defect families, one gate:

B-01  SlashCommand.isMcp was written but never read: an MCP row tap fell into
      executeSlashCommand's action dispatch, whose unrecognized-id arm returns
      "" — WIPING the composer instead of filling it. ChatInputArea also gated
      the installed-row divider + IME restore on a bare isSkill test.
B-02  selectionTableActions was wired into MinisMarkdownTextToolbar only — the
      main-chat MinisSelectionToolbarHost (the long-press toolbar chat users
      actually see) never surfaced Copy Table / Copy Table Image. The table
      registry was keyed by the bare messageId: same-message tables overwrote
      each other and a table leaving composition dropped its sibling's entry.
B-06  The swipe-up release branch re-derived eligibility inside an already
      hasText-guarded block (a dead branch) and read the debounced VM string,
      so attachment-only drags went to the keyboard and a just-typed prefix
      could be lost on release. One shared predicate read at call time now
      drives all three decision sites, and the threshold reads through
      rememberUpdatedState so ChatTuning adjustments reach the gesture.

Checks run on comment-stripped, string-blanked text, so prose in a KDoc or a
log literal cannot satisfy them:

  ChatModels.kt
    A1  every boolean field of the SlashCommand data class has >= 1 read point
        (`.field` in a condition across ChatSlashTokenExt + ChatInputArea).
        Fail-closed: a written-but-never-read boolean = the B-01 class again.
  ChatSlashTokenExt.kt
    A2  executeSlashCommand's body contains an `if (cmd.isMcp)` branch routed
        through the shared fill helper.
  ChatInputArea.kt
    A3  isMcp is read in a condition (divider / IME restore), not just defined.
  MinisTextKitGesture.kt
    B1  the file calls selectionTableActions() (the main-chat wiring).
  MinisMarkdownTextToolbar.kt
    B2  the file still calls selectionTableActions() — that path is a
        legitimate consumer; deleting it is a regression, not a cleanup.
  MinisTextKitSelection.kt
    B3  the table registry carries a table-level key: rememberTableActions /
        forgetTableActions each take an Int table dimension beyond messageId,
        and the map declaration is two-level.
    B4  selectionTableActions still guards on singleMessageId() (cross-message
        selections expose no table actions).
  StreamingMarkdownText.kt
    B5  the publisher passes the table index (3-arg rememberTableActions call)
        and sources it from the shard allocator.
  ChatInputArea.kt (swipe block, delimited by the pointerInput lambda)
    C1  exactly one shared send-eligibility predicate (`: () -> Boolean = {`)
        whose body reads the field's own TextFieldValue (never the debounced
        VM string) + the attachment list.
    C2  the predicate is used >= 3 times (the three decision sites).
    C3  the nested dead-branch shape is banned: `if (X && Y)` followed within
        4 lines by `val _ = Y || ...` — an eligibility re-derivation inside a
        branch that already implies it. Detected on the SHAPE (variable names
        are captured from the enclosing condition), not on names.
    C4  the swipe threshold reads through rememberUpdatedState (a plain val
        capture freezes the first composition's px forever), and the gesture
        reads its .value.
    C5  performSendOrEnqueue() still reached from the gesture (streaming
        enqueue preserved).
    C6  the empty-input keyboard shortcut (!inputFocused) survived.

Boundary (documented, not a bug): C4 pins the rememberUpdatedState decision —
a refactor that swaps it for a state-holder class must rename the criterion.
C3's 4-line window is the static approximation of "inside the guarded block";
a dead branch more than 4 lines below its guard needs the window widened.

Usage: python3 scripts/scan/chat_ui_interaction_check.py [repo_root]
       python3 scripts/scan/chat_ui_interaction_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import shutil
import sys
import tempfile

CHAT = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
MODELS_REL = CHAT + "/ChatModels.kt"
TOKEN_REL = CHAT + "/ChatSlashTokenExt.kt"
INPUT_REL = CHAT + "/ChatInputArea.kt"
GESTURE_REL = CHAT + "/MinisTextKitGesture.kt"
SELECTION_REL = CHAT + "/MinisTextKitSelection.kt"
STREAM_REL = CHAT + "/StreamingMarkdownText.kt"
MDTOOLBAR_REL = CHAT + "/MinisMarkdownTextToolbar.kt"

FILL_HELPER = "fillComposerWithSlashTitle("
PRED_RE = re.compile(r"val (\w+):\s*\(\)\s*->\s*Boolean\s*=\s*\{")
IF_AND_RE = re.compile(r"if\s*\((\w+)\s*&&\s*(\w+)")


# ── scanners (same provenance as the other gates: comment/string aware) ──────

def strip_comments(text):
    """Blank out comment content, keeping everything else, with a scanner that
    understands Kotlin string templates (a `//` inside a URL literal or a
    quote nested inside a `${…}` desyncs any naive scanner)."""
    out = []
    stack = [["code", 0]]
    i = 0
    n = len(text)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if text.startswith('"""', i):
                stack.append(["raw", 0]); i += 3; continue
            c = text[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            if c == "{":
                st[1] += 1
            elif c == "}":
                if st[1] == 0 and len(stack) > 1:
                    stack.pop(); out.append(c); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = text[i]
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2; continue
            if text.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        if text.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(text[i]); i += 1
    return "".join(out)


def strip_strings(code):
    """Blank the contents of string literals in comment-free code, so a whole-
    file call-site scan is never driven by a log literal."""
    out = []
    stack = [["code", 0]]
    i = 0
    n = len(code)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if code.startswith('"""', i):
                stack.append(["raw", 0]); out.append("   "); i += 3; continue
            c = code[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if len(stack) > 1 and c == "{":
                st[1] += 1
            elif len(stack) > 1 and c == "}":
                if st[1] == 0:
                    stack.pop(); out.append(c); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = code[i]
            if c == "\\":
                out.append("  ")
                i += 2
                continue
            if code.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c if c == "\n" else " "); i += 1; continue
        if code.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(code[i] if code[i] == "\n" else " "); i += 1
    return "".join(out)


def declaration_slice(text, marker):
    """Return the `{ ... }` body of the declaration containing `marker`
    (template/string aware). Returns "" when absent."""
    idx = text.find(marker)
    if idx == -1:
        return ""
    start = text.find("{", idx)
    if start == -1:
        return ""
    depth = 0
    tdepth = 0
    tstack = []
    in_str = False
    in_raw = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_raw:
            if text.startswith('"""', i):
                in_raw = False; i += 3; continue
            i += 1; continue
        if in_str:
            if c == "\\":
                i += 2; continue
            if text.startswith("${", i):
                tstack.append(tdepth); tdepth = 0; in_str = False; i += 2; continue
            if c == '"':
                in_str = False; i += 1; continue
            i += 1; continue
        if text.startswith('"""', i):
            in_raw = True; i += 3; continue
        if c == '"':
            in_str = True; i += 1; continue
        if c == "{":
            if tstack:
                tdepth += 1
            else:
                depth += 1
        elif c == "}":
            if tstack:
                if tdepth == 0:
                    tdepth = tstack.pop(); in_str = True
                else:
                    tdepth -= 1
            else:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:]


def signature_args(text, marker):
    """Return the argument list of the `marker(...)` call/declaration nearest
    the marker (no nested parens in the targets)."""
    idx = text.find(marker)
    if idx == -1:
        return None
    op = text.find("(", idx)
    cl = text.find(")", op)
    if op == -1 or cl == -1:
        return None
    return text[op + 1:cl]


def read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── the gate ─────────────────────────────────────────────────────────────────

def check_tree(root):
    """Returns (None, [problems]) when a target file is missing, else
    (path-of-first-problem, problems)."""
    problems = []

    def miss(rel):
        problems.append(("missing:" + rel, "target file missing — fail-closed"))
        return None

    models = read(root, MODELS_REL)
    token = read(root, TOKEN_REL)
    inp = read(root, INPUT_REL)
    gesture = read(root, GESTURE_REL)
    selection = read(root, SELECTION_REL)
    stream = read(root, STREAM_REL)
    mdtoolbar = read(root, MDTOOLBAR_REL)
    for rel, src in ((MODELS_REL, models), (TOKEN_REL, token), (INPUT_REL, inp),
                     (GESTURE_REL, gesture), (SELECTION_REL, selection),
                     (STREAM_REL, stream), (MDTOOLBAR_REL, mdtoolbar)):
        if src is None:
            miss(rel)
    if problems:
        return None, problems

    models_c = strip_strings(strip_comments(models))
    token_c = strip_strings(strip_comments(token))
    inp_c = strip_strings(strip_comments(inp))
    gesture_c = strip_strings(strip_comments(gesture))
    selection_c = strip_strings(strip_comments(selection))
    stream_c = strip_strings(strip_comments(stream))
    mdtoolbar_c = strip_strings(strip_comments(mdtoolbar))

    # ── A1: every SlashCommand boolean field has >= 1 read point ────────────
    # SlashCommand is a primary-constructor-only data class (no body braces),
    # so the field list is captured from the parameter list (paren-matched) —
    # slicing from the first `{` would run into the NEXT declaration's body
    # and pick up its computed properties (isText lives on AssistantBlock).
    cmd_params = signature_args(models_c, "data class SlashCommand(")
    bool_fields = re.findall(r"val\s+(\w+)\s*:\s*Boolean", cmd_params or "")
    if not bool_fields:
        problems.append((MODELS_REL, "A1 SlashCommand declares no boolean fields — "
                         "fail-closed (descriptor shape drifted?)"))
    consumers = token_c + inp_c
    for field in bool_fields:
        reads = len(re.findall(r"\.%s\b" % re.escape(field), consumers))
        if reads == 0:
            problems.append((
                "A1:%s" % field,
                "A1 SlashCommand.%s is written but has 0 read points across the "
                "slash consumers (ChatSlashTokenExt + ChatInputArea) — the "
                "B-01 class: a written-but-never-read descriptor flag" % field,
            ))

    # ── A2: executeSlashCommand carries an isMcp branch ─────────────────────
    exec_slice = declaration_slice(token_c, "fun ChatViewModel.executeSlashCommand(")
    if not exec_slice:
        problems.append(("A2", "executeSlashCommand not found — fail-closed"))
    else:
        if "if (cmd.isMcp)" not in exec_slice:
            problems.append(("A2", "executeSlashCommand has no `if (cmd.isMcp)` "
                             "branch — MCP row taps fall into the action dispatch and "
                             "wipe the composer (B-01)"))
        if FILL_HELPER not in exec_slice:
            problems.append(("A2", "executeSlashCommand must route the "
                             "typing-aid fill through the shared helper %s (single "
                             "source for the prepend/caret semantics)" % FILL_HELPER))

    # ── A3: isMcp read in a ChatInputArea condition ─────────────────────────
    if len(re.findall(r"\.isMcp\b", inp_c)) == 0:
        problems.append(("A3", "ChatInputArea never reads isMcp — the "
                         "installed-row divider / IME restore stay skill-only and an "
                         "MCP tap drops the keyboard (B-01)"))

    # ── B1: main-chat toolbar wires the table actions ───────────────────────
    if "selectionTableActions()" not in gesture_c:
        problems.append(("B1", "MinisSelectionToolbarHost never calls "
                         "selectionTableActions() — the main-chat long-press toolbar "
                         "never surfaces Copy Table / Copy Table Image (B-02)"))

    # ── B2: the markdown-toolbar path stays a consumer ──────────────────────
    if "selectionTableActions()" not in mdtoolbar_c:
        problems.append(("B2", "MinisMarkdownTextToolbar no longer calls "
                         "selectionTableActions() — that path is a legitimate "
                         "consumer; deleting it is a regression"))

    # ── B3: table-level registry key ────────────────────────────────────────
    remember_args = signature_args(selection_c, "fun rememberTableActions(")
    forget_args = signature_args(selection_c, "fun forgetTableActions(")
    if remember_args is None or ": Int" not in remember_args or remember_args.count(",") < 2:
        problems.append(("B3", "rememberTableActions must take a table-"
                         "level key (an Int dimension beyond messageId) — a bare "
                         "messageId key lets same-message tables overwrite each "
                         "other and a dispose drop its sibling (B-02)"))
    if forget_args is None or ": Int" not in forget_args:
        problems.append(("B3", "forgetTableActions must take the table "
                         "key too — a bare messageId remove takes whatever is "
                         "stored, including a still-composed sibling table"))
    sel_slice = declaration_slice(selection_c, "fun selectionTableActions(")
    if sel_slice:
        if not re.search(r"(Map<Int|SnapshotStateMap<Int)", selection_c):
            problems.append(("B3", "the table registry declaration must be "
                             "two-level (an Int table dimension inside the per-message "
                             "map)"))
    else:
        problems.append(("B3", "selectionTableActions not found — fail-closed"))

    # ── B4: cross-message exclusion preserved ───────────────────────────────
    if sel_slice and "singleMessageId()" not in sel_slice:
        problems.append(("B4", "selectionTableActions must still guard on "
                         "singleMessageId() — a cross-message selection must not "
                         "expose single-table actions"))

    # ── B5: publisher passes the table index ────────────────────────────────
    pub_call = signature_args(stream_c, ".rememberTableActions(")
    if pub_call is None or pub_call.count(",") != 2:
        problems.append(("B5", "RenderTable must publish under the table key "
                         "(a 3-arg rememberTableActions call: messageId, tableIndex, "
                         "actions)"))
    if "LocalShardSubIndexAllocator.current" not in stream_c:
        problems.append(("B5", "RenderTable must source the table index from "
                         "the shard allocator (a stable per-slot identity within the "
                         "fragment)"))

    # ── swipe block: delimited by the pointerInput lambda ───────────────────
    g_idx = inp_c.find("pointerInput(Unit)")
    if g_idx == -1:
        problems.append(("swipe-block", "swipe gesture block not found (pointerInput(Unit)) "
                         "— fail-closed"))
        gesture_slice = ""
    else:
        gesture_slice = declaration_slice(inp_c, "pointerInput(Unit)")

    # ── C1/C2: one shared eligibility predicate, read at call time ──────────
    preds = PRED_RE.findall(inp_c)
    send_preds = [name for name in preds
                  if re.search(r"val %s:\s*\(\)\s*->\s*Boolean\s*=\s*\{([^\n]*)" % re.escape(name), inp_c)
                  and "inputFieldValue" in inp_c]
    # Narrow to the predicate whose body carries the field-value + attachments reads.
    body_match = re.search(r"val (\w+):\s*\(\)\s*->\s*Boolean\s*=\s*\{\s*inputFieldValue", inp_c)
    if body_match is None:
        problems.append(("C1", "no shared send-eligibility predicate found — the "
                         "three swipe decision sites must read one predicate "
                         "(`val <name>: () -> Boolean = { inputFieldValue.text.isNotBlank()"
                         " || attachments.isNotEmpty() }`), not re-derive it per site (B-06)"))
    else:
        name = body_match.group(1)
        # The body spans lines — capture from the opening brace to the matching
        # close (same-line capture would see only the newline).
        brace = inp_c.find("{", body_match.start())
        close = inp_c.find("}", brace)
        pred_body = inp_c[brace:close] if brace != -1 and close != -1 else ""
        if "inputText.isNotBlank()" in pred_body:
            problems.append((INPUT_REL, "C1 the swipe predicate reads the debounced VM "
                             "string (inputText) — a just-typed prefix is invisible for "
                             "the debounce window; it must read the field's own "
                             "TextFieldValue"))
        if "attachments" not in pred_body:
            problems.append(("C1", "C1 the swipe predicate ignores attachments — "
                             "attachment-only drags must count as sendable (B-06)"))
        calls = len(re.findall(r"\b%s\(\)" % re.escape(name), inp_c))
        if calls < 3:
            problems.append(("C2", "C2 the shared predicate is used %d time(s) — the "
                             "three decision sites (drag hint / release-send / empty-input "
                             "keyboard) must all read it, or a per-site re-derivation "
                             "drifts back" % calls))

    # ── C3: nested dead-branch shape (shape-detected, not name-bound) ───────
    lines = gesture_slice.split("\n")
    for i, line in enumerate(lines):
        m = IF_AND_RE.search(line)
        if not m:
            continue
        second = m.group(2)
        window = [l for l in lines[i + 1:i + 5] if l.strip()]
        for l in window:
            if re.search(r"val\s+\w+\s*=\s*%s\b\s*(\(\))?\s*\|\|" % re.escape(second), l):
                problems.append(("C3", "dead branch: an eligibility value is "
                                 "re-derived from `%s` inside a block already guarded by "
                                 "`&& %s` — the inner test is tautological and routes "
                                 "attachment-only drags to the keyboard (B-06)" % (second, second)))
                break

    # ── C4: threshold via rememberUpdatedState ──────────────────────────────
    thr = re.search(r"val (\w+)\s*=\s*rememberUpdatedState\(\s*with\(LocalDensity\.current\)\s*\{\s*sendSwipeThresholdDp\.dp\.toPx\(\)", inp_c)
    if thr is None:
        problems.append(("C4", "the swipe threshold must read through "
                         "rememberUpdatedState — a plain val capture freezes the first "
                         "composition's px and a ChatTuning adjustment never reaches the "
                         "gesture"))
    else:
        thr_name = thr.group(1)
        if "%s.value" % thr_name not in gesture_slice:
            problems.append(("C4", "the gesture must read the threshold state "
                             "(%s.value), or the state holder is dead weight" % thr_name))

    # ── C5/C6: preserved behaviors inside the gesture ───────────────────────
    if gesture_slice:
        if "performSendOrEnqueue()" not in gesture_slice:
            problems.append(("C5", "the release branch must still route through "
                             "performSendOrEnqueue() — streaming drag-to-send enqueues "
                             "instead of being dropped"))
        if "!inputFocused" not in gesture_slice:
            problems.append(("C6", "the empty-input keyboard shortcut "
                             "(!inputFocused) must survive — swiping up with nothing to "
                             "send and no focus pops the keyboard"))

    return INPUT_REL, problems


# ── selftest fixtures ────────────────────────────────────────────────────────

MODELS_F = """package com.rikkaminis.app.ui.chat

@Immutable
data class SlashCommand(
    val id: String,
    val icon: androidx.compose.ui.graphics.vector.ImageVector,
    val title: String,
    val subtitle: String,
    val isSkill: Boolean = false,
    val isMcp: Boolean = false,
)
"""

TOKEN_F = """package com.rikkaminis.app.ui.chat

fun ChatViewModel.executeSlashCommand(cmd: SlashCommand, currentInput: String = ""): String {
    val saved = savedInputBeforeSlash
    if (cmd.isSkill) {
        resetSlashMenuState()
        return fillComposerWithSlashTitle(cmd, saved)
    }
    if (cmd.isMcp) {
        resetSlashMenuState()
        return fillComposerWithSlashTitle(cmd, saved)
    }
    when (cmd.id) {
        "memory" -> toggleMemoryEnabled()
        else -> Unit
    }
    return ""
}
"""

INPUT_F = """package com.rikkaminis.app.ui.chat

@Composable
fun ChatInputArea() {
    val swipeThresholdPxState = rememberUpdatedState(
        with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() },
    )
    val canSwipeSendNow: () -> Boolean = {
        inputFieldValue.text.isNotBlank() || attachments.isNotEmpty()
    }
    itemsIndexed(filteredSlashCommands, key = { _, c -> c.id }) { index, cmd ->
        val isInstalledRow = cmd.isSkill || cmd.isMcp
        val prevIsInstalledRow = index > 0 &&
            (filteredSlashCommands[index - 1].isSkill ||
                filteredSlashCommands[index - 1].isMcp)
        if (isInstalledRow && index > 0 && !prevIsInstalledRow) {
            HorizontalDivider()
        }
        if (cmd.isSkill || cmd.isMcp) {
            inputFocusRequester.requestFocus()
        }
    }
    Box(modifier = Modifier
        .fillMaxWidth()
        .pointerInput(Unit) {
            val slop = viewConfiguration.touchSlop
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false)
                var totalDy = 0f
                var claimed = false
                verticalDrag(down.id) { change ->
                    if (totalDy < 0) {
                        if (canSwipeSendNow()) {
                            val newProgress = (-totalDy / swipeThresholdPxState.value).coerceIn(0f, 1f)
                            sendSwipeProgress = newProgress
                        } else if (sendSwipeProgress != 0f) {
                            sendSwipeProgress = 0f
                        }
                    }
                }
                val swipedUp = claimed && totalDy < 0
                if (swipedUp && canSwipeSendNow()) {
                    if (sendSwipeProgress >= swipeArmFraction) {
                        performSendOrEnqueue()
                    }
                    sendSwipeProgress = 0f
                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {
                    inputFocusRequester.requestFocus()
                    sendSwipeProgress = 0f
                } else {
                    sendSwipeProgress = 0f
                }
            }
        },
    ) {
        Column {
        }
    }
}
"""

GESTURE_F = """package com.rikkaminis.app.ui.chat

@Composable
fun MinisSelectionToolbarHost(controller: SelectionController) {
    Row {
        MinisToolbarButton(label = "Copy") { controller.clearSelection() }
        val tableActions = controller.selectionTableActions()
        if (tableActions != null) {
            MinisToolbarButton(label = "Copy Table") { tableActions.copyTableMarkdown() }
            MinisToolbarButton(label = "Copy Table Image") { tableActions.copyTableImage() }
        }
    }
}
"""

SELECTION_F = """package com.rikkaminis.app.ui.chat

class SelectionController {
    class TableActions(
        val copyTableMarkdown: () -> Unit,
        val copyTableImage: () -> Unit,
    )
    private val tableActionsByMessage =
        androidx.compose.runtime.mutableStateMapOf<
            String,
            androidx.compose.runtime.snapshots.SnapshotStateMap<Int, TableActions>
        >()
    fun rememberTableActions(messageId: String, tableIndex: Int, actions: TableActions) {
        val perMessage = tableActionsByMessage.getOrPut(messageId) {
            androidx.compose.runtime.mutableStateMapOf()
        }
        perMessage[tableIndex] = actions
    }
    fun forgetTableActions(messageId: String, tableIndex: Int) {
        val perMessage = tableActionsByMessage[messageId] ?: return
        perMessage.remove(tableIndex)
        if (perMessage.isEmpty()) tableActionsByMessage.remove(messageId)
    }
    fun selectionTableActions(): TableActions? {
        val msgId = singleMessageId() ?: return null
        val perMessage = tableActionsByMessage[msgId] ?: return null
        for (endpoint in listOf(selection.value.start, selection.value.end)) {
            val tableIndex = tableIndexFromShard(endpoint.shard.shardId) ?: continue
            perMessage[tableIndex]?.let { return it }
        }
        return null
    }
}
"""

STREAM_F = """package com.rikkaminis.app.ui.chat

@Composable
private fun RenderTable(block: MdBlock.Table) {
    val selectionController = LocalMinisSelectionController.current
    val shardIdForTable = LocalShardId.current
    val messageId = shardIdForTable?.messageId
    val tableSlotAllocator = LocalShardSubIndexAllocator.current
    val tableIndex = remember { tableSlotAllocator?.next() ?: 0 }
    if (selectionController != null && messageId != null) {
        androidx.compose.runtime.DisposableEffect(selectionController, messageId, block.raw, tableIndex) {
            selectionController.rememberTableActions(messageId, tableIndex, actions)
            onDispose { selectionController.forgetTableActions(messageId, tableIndex) }
        }
    }
}
"""

MDTOOLBAR_F = """package com.rikkaminis.app.ui.chat

@Composable
fun MinisMarkdownTextToolbarHost(toolbar: Toolbar) {
    Row {
        ToolbarButton(label = "Copy") { toolbar.hide() }
        val tableActions = toolbar.selectionController?.selectionTableActions()
        if (tableActions != null) {
            ToolbarButton(label = "Copy Table") { tableActions.copyTableMarkdown() }
        }
    }
}
"""


def fixtures(**overrides):
    files = {
        MODELS_REL: MODELS_F,
        TOKEN_REL: TOKEN_F,
        INPUT_REL: INPUT_F,
        GESTURE_REL: GESTURE_F,
        SELECTION_REL: SELECTION_F,
        STREAM_REL: STREAM_F,
        MDTOOLBAR_REL: MDTOOLBAR_F,
    }
    files.update(overrides)
    return files


def selftest():
    failures = []

    def expect(name, files, want_violation_ids):
        root = tempfile.mkdtemp(prefix="chat-ui-gate-selftest-")
        try:
            for rel, content in files.items():
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            _, problems = check_tree(root)
            got = {cid for cid, _ in problems}
            if want_violation_ids is None:
                if problems:
                    failures.append("%s: expected CLEAN, got %s" % (name, sorted(got)))
            else:
                missing = [v for v in want_violation_ids
                           if not any(v in cid for cid in got)]
                if missing:
                    failures.append("%s: expected %s, got %s" %
                                    (name, want_violation_ids, sorted(got)))
        finally:
            shutil.rmtree(root)

    # clean fixture → exit 0
    expect("clean", fixtures(), None)
    # A1: a written-but-never-read boolean flips red (the B-01 class, generalized)
    expect("A1 unread boolean", fixtures(**{
        MODELS_REL: MODELS_F.replace(
            "    val isMcp: Boolean = false,\n",
            "    val isMcp: Boolean = false,\n    val isZombie: Boolean = false,\n"),
    }), ["A1"])
    # A2: dropping the isMcp branch flips red
    expect("A2 no mcp branch", fixtures(**{
        TOKEN_REL: TOKEN_F.replace(
            "    if (cmd.isMcp) {\n        resetSlashMenuState()\n"
            "        return fillComposerWithSlashTitle(cmd, saved)\n    }\n", ""),
    }), ["A2"])
    # B1: dropping the main-chat wiring flips red
    expect("B1 gesture call dropped", fixtures(**{
        GESTURE_REL: GESTURE_F.replace(
            "        val tableActions = controller.selectionTableActions()\n"
            "        if (tableActions != null) {\n"
            "            MinisToolbarButton(label = \"Copy Table\") { tableActions.copyTableMarkdown() }\n"
            "            MinisToolbarButton(label = \"Copy Table Image\") { tableActions.copyTableImage() }\n"
            "        }\n", ""),
    }), ["B1"])
    # B2: deleting the markdown-toolbar consumer flips red
    expect("B2 mdtoolbar consumer dropped", fixtures(**{
        MDTOOLBAR_REL: MDTOOLBAR_F.replace(
            "        val tableActions = toolbar.selectionController?.selectionTableActions()\n"
            "        if (tableActions != null) {\n"
            "            ToolbarButton(label = \"Copy Table\") { tableActions.copyTableMarkdown() }\n"
            "        }\n", ""),
    }), ["B2"])
    # B3: reverting the key to a bare messageId flips red
    expect("B3 key reverted", fixtures(**{
        SELECTION_REL: SELECTION_F.replace(
            "    fun rememberTableActions(messageId: String, tableIndex: Int, actions: TableActions) {",
            "    fun rememberTableActions(messageId: String, actions: TableActions) {").replace(
            "    fun forgetTableActions(messageId: String, tableIndex: Int) {",
            "    fun forgetTableActions(messageId: String) {"),
    }), ["B3"])
    # B5: publishing without the table index flips red
    expect("B5 bare-messageId publish", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "selectionController.rememberTableActions(messageId, tableIndex, actions)",
            "selectionController.rememberTableActions(messageId, actions)"),
    }), ["B5"])
    # C3: restoring the dead-branch shape flips red
    expect("C3 dead branch", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {",
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    val canSendNow = canSwipeSendNow() || attachments.isNotEmpty()\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {"),
    }), ["C3"])
    # C3 (baseline shape): the exact defect form — guard on a bare boolean whose
    # body re-derives eligibility from that boolean
    expect("C3 baseline dead branch", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val canSwipeSendNow: () -> Boolean = {\n"
            "        inputFieldValue.text.isNotBlank() || attachments.isNotEmpty()\n"
            "    }\n", "")
            .replace(
            "                        if (canSwipeSendNow()) {",
            "                        val hasText = inputText.isNotBlank()\n"
            "                        if (hasText) {")
            .replace(
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {",
            "                val hasText = inputText.isNotBlank()\n"
            "                if (swipedUp && hasText) {\n"
            "                    val canSendNow = hasText || attachmentsCount > 0\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {")
            .replace(
            "                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {",
            "                } else if (swipedUp && !hasText && !inputFocused) {"),
    }), ["C1", "C3"])
    # C4: plain val capture of the threshold flips red
    expect("C4 frozen threshold", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val swipeThresholdPxState = rememberUpdatedState(\n"
            "        with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() },\n"
            "    )\n",
            "    val swipeThresholdPxState = with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() }\n"),
    }), ["C4"])
    # C5: dropping the enqueue route flips red
    expect("C5 enqueue dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                        performSendOrEnqueue()\n", ""),
    }), ["C5"])
    # C6: dropping the keyboard shortcut flips red
    expect("C6 keyboard shortcut dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {",
            "                } else if (swipedUp && !canSwipeSendNow()) {"),
    }), ["C6"])

    if failures:
        print("❌ selftest failed:")
        for f in failures:
            print("   " + f)
        return 1
    print("CHAT_UI_INTERACTION_SELFTEST=PASS (12 fixtures: clean + 11 violation)")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root = os.path.abspath(args[0]) if args else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    if "--selftest" in flags:
        return selftest()
    unknown = [f for f in flags if f != "--selftest"]
    if unknown:
        print("unknown option(s): %s" % " ".join(unknown))
        return 2
    path, problems = check_tree(root)
    if path is None or problems:
        print("chat_ui_interaction_check: %d violation(s) in %s" % (len(problems), path))
        for cid, msg in problems:
            print("  [%s] %s" % (cid, msg))
        return 1
    print("chat_ui_interaction_check: slash wiring + table toolbar + swipe parity — "
          "CHAT-UI-INTERACTION GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
