#!/usr/bin/env python3
"""Stale-closure guard — RikkaMinis CI scan gate.

## Why this gate exists

2026-09-20, for real: FIX-4 (7d603596) fixed a broken throttle in
`StreamingMarkdownTextBody` by dropping `content` from the `produceState` key
list. The throttle started working — and the streaming text froze at whatever
was on screen when the row first composed. The user saw the first two
characters of an answer and nothing else, until they left the session and came
back.

The mechanism is a Compose footgun with three parts:

  1. `produceState`'s keys ARE `LaunchedEffect` keys (`remember(key)`), so a
     key change cancels and restarts the producer coroutine.
  2. When the key does NOT change, the producer closure is NOT rebuilt — it
     keeps whatever it captured when it was created.
  3. `snapshotFlow { x }` re-emits only for values it can OBSERVE through the
     snapshot system. Reading a plain parameter (`content: String`) registers
     no dependency, so the flow emits exactly ONCE.

So: a long-lived effect (produceState / LaunchedEffect / rememberCoroutineScope
launch) whose body reads a plain parameter that the CALLER updates every
recomposition — without `rememberUpdatedState` — silently goes stale. Nothing
crashes, no test fails, and the UI just stops updating.

The fix is always the same: route the changing value through
`rememberUpdatedState` and read the returned State inside the effect.

## What this gate flags

For every `produceState(...)` call in production sources, take the parameter
names of the enclosing function. If the producer body reads one of them
directly (not via `rememberUpdatedState`, not via a `State`-typed source, not
via `.value`), the value can go stale when that parameter is removed from the
key list. `rememberUpdatedState(x)` on the same name clears the finding.

Escape hatch: `stale-ok: <reason>` on the same or the previous line.

Usage: python3 scripts/scan/stale_closure_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s) found
"""
import os
import re
import sys

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
ESCAPE_RE = re.compile(r"stale-ok\s*:")
LINE_COMMENT_RE = re.compile(r"//.*$")

# 只检查这些「长生命周期 effect」构造器
EFFECT_RE = re.compile(r"\bproduceState\s*(?:<[^>]*>)?\s*\(")

# 生产源码根（相对 repo root）
SRC_ROOTS = [
    "src/android/app/src/main/java",
]

# 已知安全的调用点：effect 内部不读任何会变的普通参数。
# 每条都必须写明为什么安全。
ALLOWED = {
    # 读的是 State 支撑的值或常量，与参数变化无关
    "com/rikkaminis/app/ui/chat/ChatMiscViews.kt": "reads block.id/tabPool/lifecycleOwner, all stable keys",
}


def strip_comments(text):
    text = BLOCK_COMMENT_RE.sub("", text)
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            out.append(line if ESCAPE_RE.search(line) else "//")
        else:
            out.append(LINE_COMMENT_RE.sub("", line))
    return out


def enclosing_params(lines, idx):
    """向上找最近的函数签名，返回 (参数名集合, 签名起始行)。"""
    for i in range(idx, max(-1, idx - 120), -1):
        line = lines[i]
        if re.search(r"\bfun\s+\w+\s*[(<]", line) or re.search(r"^\s*(private |internal |public )?\bfun\b", line):
            # 收集签名（可能跨行）直到匹配的 ')'
            sig = []
            depth = 0
            started = False
            for j in range(i, min(len(lines), i + 60)):
                sig.append(lines[j])
                depth += lines[j].count("(") - lines[j].count(")")
                if "(" in lines[j]:
                    started = True
                if started and depth <= 0:
                    break
            blob = " ".join(sig)
            params = set()
            for m in re.finditer(r"(\w+)\s*:\s*[A-Za-z_][\w<>,.?\s]*", blob):
                params.add(m.group(1))
            return params, i
    return set(), -1


def effect_body(lines, start_idx):
    """取出 produceState 调用的 lambda 体与 key 实参文本。

    produceState(initialValue = X, key1, key2, ...) { block }
      - 第一个实参 initialValue 不是 key
      - 最后一个实参是 block（从第一个 '{' 开始）
    只有中间的 key 实参能阻止闭包 stale。返回 (body行, 行区间, key文本)。
    """
    depth = 0
    begun = False
    body = []
    key_parts = []
    # 收集调用头：从 produceState( 之后到第一个 '{' 之前
    for j in range(start_idx, min(len(lines), start_idx + 200)):
        line = lines[j]
        body.append(line)
        if not begun:
            key_parts.append(line)
        depth += line.count("{") - line.count("}")
        depth += line.count("(") - line.count(")")
        if "{" in line:
            begun = True
        if begun and depth <= 0:
            return body, (start_idx, j), split_key_args("\n".join(key_parts))
    return body, (start_idx, min(len(lines), start_idx + 200)), split_key_args("\n".join(key_parts))


def split_key_args(head: str) -> str:
    """从调用头里剥掉第一个实参（initialValue），只保留 key 实参。

    head 形如：`val x by produceState(initialValue = content, isStreaming) {`
    """
    open_idx = head.find("(")
    if open_idx < 0:
        return head
    inner = head[open_idx + 1:]
    # 按顶层逗号切分
    parts = []
    depth = 0
    cur = []
    for ch in inner:
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    # 去掉 block 起始（含 '{'）之后的残余
    cleaned = []
    for p in parts:
        if "{" in p:
            p = p[: p.index("{")]
        p = p.strip()
        if p:
            cleaned.append(p)
    # 第一个是 initialValue，不是 key
    return ", ".join(cleaned[1:]) if len(cleaned) > 1 else ""


def scan_file(path, rel):
    raw = open(path, encoding="utf-8", errors="replace").read()
    lines = strip_comments(raw)
    findings = []
    for i, line in enumerate(lines):
        if not EFFECT_RE.search(line):
            continue
        params, _sig = enclosing_params(lines, i)
        if not params:
            continue
        body, (b0, b1), key_text = effect_body(lines, i)
        body_text = "\n".join(body)
        # 本 effect 里被 rememberUpdatedState 包装过的名字 = 安全
        wrapped = set(re.findall(r"rememberUpdatedState\s*\(\s*(\w+)\s*\)", body_text))
        # 也扫描 effect 之前同一函数内的包装（生产写法是先 wrap 再用）
        for j in range(max(0, i - 40), i):
            wrapped |= set(re.findall(r"rememberUpdatedState\s*\(\s*(\w+)\s*\)", lines[j]))
        for p in sorted(params):
            if p in wrapped:
                continue
            # 参数出现在 key 列表里 ⇒ 每次变化都重启闭包 ⇒ 不会 stale
            if re.search(r"(?<![\w.])" + re.escape(p) + r"(?![\w:])", key_text):
                continue
            # 直接读该参数（裸标识符，非 .value / 非声明）
            if re.search(r"(?<![\w.])" + re.escape(p) + r"(?![\w:])", body_text):
                exempt = False
                for k in range(max(0, b0 - 1), min(len(lines), b1 + 2)):
                    if ESCAPE_RE.search(lines[k]):
                        exempt = True
                if exempt:
                    continue
                findings.append((i + 1, p))
    return findings


# ─────────────────────────────────────────────────────────────────────────
# [B-03 deep-audit-1037] bottom-verdict section
#
# 2026-10 audit: the DragInteraction.Stop handler in ChatScreen.kt compared
# the viewport-TOP index (`firstVisibleItemIndex`) against `total - 2` — a
# reversed formula left over from the reverseLayout era. On a forward list
# with a multi-row viewport it read FALSE at the true bottom (streaming
# stopped following after a drag) and TRUE when a tall last item's top was
# at the viewport top (a reader got yanked back). The same audit found
# `isNearBottom`'s `remember(listState, nearBottomThresholdPx)` captured the
# threshold but never consumed it in the body — the scrollNearBottomDp knob
# was dead on the fallback arm.
#
# Three criteria (self-test carries one breaker each):
#   S1  the DragStop branch must not reference the reversed-era
#       viewport-top formula family (firstVisibleItemIndex /
#       firstVisibleItemScrollOffset);
#   S2  `nearBottomThresholdPx` must be consumed inside the remember body
#       whose key list carries it (captured-but-unused = this gate's family);
#   S3  the bottom verdict has ONE implementation (`isListAtBottom`) defined
#       once and called from both consumers.
# ─────────────────────────────────────────────────────────────────────────

SCREEN_FILE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"

def code_only(text):
    """Blank comments and string-literal contents (spaces keep line/col).

    Braces inside strings (JSON builders, string templates) are blanked too,
    so brace matching and identifier scans only see real code. Handles
    escapes, char literals and triple-quoted raw strings.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            seg = text[i:j]
            out.append("".join(c if c == "\n" else " " for c in seg))
            i = j
            continue
        if ch == '"':
            if text[i:i + 3] == '"""':
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            seg = text[i:j]
            out.append("".join(c if c == "\n" else " " for c in seg))
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ("'", "\n"):
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)

def extract_block(lines, sig_re):
    """Brace-matched block starting at the first line matching sig_re.

    The body brace is the first `{` at paren level 0 AFTER the signature's
    parameter list closes — lambda defaults inside the parameter list
    (`onCopy: () -> Unit = {}`) sit at paren level 1 and are skipped, which a
    naive "first brace opens the body" walk gets wrong.

    Returns (start, end) inclusive line indices, or (-1, -1).
    """
    for i, ln in enumerate(lines):
        if not sig_re.search(ln):
            continue
        paren = 0
        body_line = -1
        j = i
        while j < len(lines) and body_line < 0:
            line = lines[j]
            k = 0
            while k < len(line):
                c = line[k]
                if c == "(":
                    paren += 1
                elif c == ")":
                    paren -= 1
                elif c == "{" and paren <= 0:
                    body_line = j
                    break
                k += 1
            j += 1
        if body_line < 0:
            return i, len(lines) - 1
        depth = 0
        for j2 in range(body_line, len(lines)):
            depth += lines[j2].count("{") - lines[j2].count("}")
            if depth <= 0:
                return i, j2
        return i, len(lines) - 1
    return -1, -1

STOP_BRANCH_RE = re.compile(
    r"is\s+androidx\.compose\.foundation\.interaction\.DragInteraction\.Stop\s*->"
)
REVERSED_FORMULA_RE = re.compile(
    r"firstVisibleItemIndex|firstVisibleItemScrollOffset"
)

def scan_bottom_verdict(root):
    """[B-03 deep-audit-1037] S1/S2/S3. Skipped when ChatScreen.kt is absent
    (synthetic fixture trees); fail-closed once the file exists."""
    findings = []
    path = os.path.join(root, SCREEN_FILE)
    if not os.path.exists(path):
        return findings
    raw = open(path, encoding="utf-8", errors="replace").read()
    code = code_only(raw)
    lines = code.split("\n")

    # S1: DragStop branch free of the reversed-era formula
    s0, s1 = extract_block(lines, STOP_BRANCH_RE)
    if s0 < 0:
        findings.append(
            "ChatScreen.kt:?  [B-03 S1] DragInteraction.Stop branch NOT FOUND (fail-closed)"
        )
    else:
        branch = "\n".join(lines[s0:s1 + 1])
        if REVERSED_FORMULA_RE.search(branch):
            findings.append(
                "ChatScreen.kt:%d  [B-03 S1] reversed-era viewport-top formula "
                "(firstVisibleItemIndex/offset) back in the DragStop branch — it reads "
                "FALSE at the true bottom and TRUE when a tall last item's top is at "
                "the viewport top" % (s0 + 1)
            )

    # S2: the threshold is consumed inside its remember body
    rem_re = re.compile(r"remember\s*\([^)]*nearBottomThresholdPx[^)]*\)")
    r_idx = next((i for i, ln in enumerate(lines) if rem_re.search(ln)), -1)
    if r_idx < 0:
        findings.append(
            "ChatScreen.kt:?  [B-03 S2] remember(...nearBottomThresholdPx...) NOT FOUND (fail-closed)"
        )
    else:
        b0, b1 = extract_block(lines, rem_re)
        region = "\n".join(lines[r_idx:b1 + 1]) if b0 >= 0 else lines[r_idx]
        n_hits = len(re.findall(r"nearBottomThresholdPx", region))
        # 1 hit = the key list only (captured but never consumed)
        if n_hits < 2:
            findings.append(
                "ChatScreen.kt:%d  [B-03 S2] nearBottomThresholdPx captured in the "
                "remember key list but never consumed in the body — the "
                "scrollNearBottomDp knob is dead (this gate's own family)" % (r_idx + 1)
            )

    # S3: one bottom implementation, two consumers
    defs = [i for i, ln in enumerate(lines) if re.search(r"fun\s+isListAtBottom\s*\(", ln)]
    total_calls = sum(ln.count("isListAtBottom(") for ln in lines)
    if len(defs) != 1:
        findings.append(
            "ChatScreen.kt:?  [B-03 S3] isListAtBottom defined %d time(s) — the bottom "
            "verdict must have exactly one implementation" % len(defs)
        )
    elif total_calls - 1 < 2:
        findings.append(
            "ChatScreen.kt:?  [B-03 S3] isListAtBottom called %d time(s) — both "
            "consumers (isNearBottom + DragStop) must route through the shared "
            "verdict" % (total_calls - 1)
        )
    return findings

# ─────────────────────────────────────────────────────────────────────────
# [B-05 deep-audit-1037] visible-content section
#
# Five entry points ask "does this user row show anything?"; each carried its
# own copy (images-only / content-only / text-parts-only) so a pure
# non-image attachment message vanished on reload, could not be retried, and
# shifted the retry/edit cutoff. The fix routes all five through ONE shared
# predicate. Three criteria:
#   V1  the predicate has exactly one definition (not five copies);
#   V2  each of the five call sites references the shared predicate
#       (per-file fail-closed when the file exists);
#   V3  the queued withdraw button is no longer gated by the caption
#       (it sits outside the content-only block).
# ─────────────────────────────────────────────────────────────────────────

VC_ROOT = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
VC_DEF_FILE = VC_ROOT + "/ChatTranscriptRebuild.kt"
VC_FILES = {
    "transcript": VC_ROOT + "/ChatTranscriptRebuild.kt",
    "viewmodel": VC_ROOT + "/ChatViewModel.kt",
    "queue": VC_ROOT + "/ChatQueueInterruption.kt",
    "bubble": VC_ROOT + "/ChatUserMessageUI.kt",
}

def scan_visible_content(root):
    findings = []
    def_path = os.path.join(root, VC_DEF_FILE)
    code_cache = {}

    def load(rel):
        rel = VC_FILES.get(rel, rel)
        if rel not in code_cache:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                code_cache[rel] = None
            else:
                code_cache[rel] = code_only(open(p, encoding="utf-8", errors="replace").read())
        return code_cache[rel]

    # V1: exactly one definition of each shared predicate (anchor file present)
    if os.path.exists(def_path):
        def_lines = load("transcript").split("\n")
        for pred in ("userRowHasVisibleContent", "userPartsJsonHasVisibleContent"):
            n = len([1 for ln in def_lines if re.search(r"fun\s+" + pred + r"\s*\(", ln)])
            if n != 1:
                findings.append(
                    "ChatTranscriptRebuild.kt:?  [B-05 V1] %s defined %d time(s) — the "
                    "visible-content verdict must have exactly one implementation" % (pred, n)
                )

    # V2: five call sites reference the shared predicate
    t_code = load("transcript")
    if t_code is not None:
        t_lines = t_code.split("\n")
        n_ref = sum(ln.count("userRowHasVisibleContent(") for ln in t_lines)
        n_def = len([1 for ln in t_lines if re.search(r"fun\s+userRowHasVisibleContent\s*\(", ln)])
        if n_ref - n_def < 1:
            findings.append(
                "ChatTranscriptRebuild.kt:?  [B-05 V2] transcript skip guard no longer calls "
                "userRowHasVisibleContent — pure-attachment rows vanish on reload"
            )
    vm_code = load("viewmodel")
    if vm_code is not None:
        i0, i1 = extract_block(vm_code.split("\n"), re.compile(r"fun\s+retryFromMessage\s*\("))
        if i0 < 0:
            findings.append(
                "ChatViewModel.kt:?  [B-05 V2] retryFromMessage body NOT FOUND (fail-closed)"
            )
        else:
            body = "\n".join(vm_code.split("\n")[i0:i1 + 1])
            if "userRowHasVisibleContent(" not in body:
                findings.append(
                    "ChatViewModel.kt:%d  [B-05 V2] retryFromMessage guard no longer calls "
                    "userRowHasVisibleContent — pure-attachment messages can't be retried" % (i0 + 1)
                )
            if "userPartsJsonHasVisibleContent(" not in body:
                findings.append(
                    "ChatViewModel.kt:%d  [B-05 V2] retry DB-walk no longer calls "
                    "userPartsJsonHasVisibleContent — the cutoff drifts from the edit path" % (i0 + 1)
                )
    q_code = load("queue")
    if q_code is not None:
        i0, i1 = extract_block(q_code.split("\n"), re.compile(r"fun\s+ChatViewModel\.truncateBeforeEdit\s*\("))
        if i0 < 0:
            findings.append(
                "ChatQueueInterruption.kt:?  [B-05 V2] truncateBeforeEdit body NOT FOUND (fail-closed)"
            )
        else:
            body = "\n".join(q_code.split("\n")[i0:i1 + 1])
            if "userPartsJsonHasVisibleContent(" not in body:
                findings.append(
                    "ChatQueueInterruption.kt:%d  [B-05 V2] edit DB-walk no longer calls "
                    "userPartsJsonHasVisibleContent — the cutoff drifts from the retry path" % (i0 + 1)
                )
    b_code = load("bubble")
    if b_code is not None:
        lines = b_code.split("\n")
        i0, i1 = extract_block(lines, re.compile(r"fun\s+UserMessageBubble\s*\("))
        if i0 < 0:
            findings.append(
                "ChatUserMessageUI.kt:?  [B-05 V2] UserMessageBubble body NOT FOUND (fail-closed)"
            )
        else:
            body = "\n".join(lines[i0:i1 + 1])
            if "userRowHasVisibleContent(" not in body:
                findings.append(
                    "ChatUserMessageUI.kt:%d  [B-05 V2] bubble row gating no longer calls "
                    "userRowHasVisibleContent — pure-attachment queued messages lose the "
                    "withdraw target" % (i0 + 1)
                )
            # V3: the withdraw button sits outside the caption-only block
            w_idx = next(
                (k for k in range(i0, i1 + 1) if "chat_withdraw_queued" in lines[k]),
                -1,
            )
            if w_idx < 0:
                findings.append(
                    "ChatUserMessageUI.kt:%d  [B-05 V3] queued withdraw button NOT FOUND "
                    "(fail-closed)" % (i0 + 1)
                )
            else:
                # (a) the button's own guard must not re-introduce the caption gate
                cond_idx = -1
                for k in range(w_idx, i0 - 1, -1):
                    if re.search(r"\bif\s*\(", lines[k]):
                        cond_idx = k
                        break
                if cond_idx >= 0:
                    cond = "\n".join(lines[cond_idx:w_idx + 1])
                    if "content.isNotBlank(" in cond:
                        findings.append(
                            "ChatUserMessageUI.kt:%d  [B-05 V3] the queued withdraw button is "
                            "gated by the caption again — a pure-attachment queued message "
                            "loses its withdraw target" % (cond_idx + 1)
                        )
                # (b) it must not be nested inside the caption-only text block
                cap_idx = next(
                    (k for k in range(i0, i1 + 1) if re.search(r"if\s*\(\s*message\.content\.isNotBlank\(\)\s*\)", lines[k])),
                    -1,
                )
                if cap_idx < 0:
                    findings.append(
                        "ChatUserMessageUI.kt:%d  [B-05 V3] caption-only text-bubble guard NOT "
                        "FOUND (fail-closed — an empty caption rect would render again)" % (i0 + 1)
                    )
                else:
                    c0, c1 = extract_block(lines, re.compile(r"if\s*\(\s*message\.content\.isNotBlank\(\)\s*\)"))
                    block = "\n".join(lines[c0:c1 + 1])
                    if "chat_withdraw_queued" in block:
                        findings.append(
                            "ChatUserMessageUI.kt:%d  [B-05 V3] the queued withdraw button is "
                            "nested inside the caption-only block — a pure-attachment queued "
                            "message loses its withdraw target" % (c0 + 1)
                        )
    return findings

# ─────────────────────────────────────────────────────────────────────────
# self-test: one breaker per criterion, expected literals hand-written from
# the rule table above (never derived from a run).
# ─────────────────────────────────────────────────────────────────────────

def self_test():
    import tempfile
    import shutil
    results = []

    def run_scan(files):
        tmp = tempfile.mkdtemp(prefix="scguard-")
        try:
            for rel, text in files.items():
                p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            return scan_bottom_verdict(tmp) + scan_visible_content(tmp)
        finally:
            shutil.rmtree(tmp)

    # Clean fixture: the post-fix shape. S1 branch without the formula,
    # S2 threshold consumed, S3 one def + two calls, V1 one def each,
    # V2 all five call sites, V3 withdraw outside the caption block.
    clean_screen = (
        "package x\n"
        "class LazyListState { val layoutInfo = 0 }\n"
        "internal fun isListAtBottom(a: Int, b: Int, c: Int, t: Int): Boolean = true\n"
        "fun Screen(listState: LazyListState) {\n"
        "    val nearBottomThresholdPx = 32f\n"
        "    val isNearBottom = remember(listState, nearBottomThresholdPx) {\n"
        "        derivedStateOf {\n"
        "            val last = 1\n"
        "            isListAtBottom(last, 2, 3, nearBottomThresholdPx.toInt())\n"
        "        }\n"
        "    }\n"
        "    LaunchedEffect(listState) {\n"
        "        listState.interactionSource.interactions.collect { interaction ->\n"
        "            when (interaction) {\n"
        "                is androidx.compose.foundation.interaction.DragInteraction.Start -> {}\n"
        "                is androidx.compose.foundation.interaction.DragInteraction.Stop -> {\n"
        "                    val info = listState.layoutInfo\n"
        "                    val sentinel = info.visibleItemsInfo.firstOrNull { it.index == info.totalItemsCount - 1 }\n"
        "                    val last = info.visibleItemsInfo.maxByOrNull { it.index }\n"
        "                    val stoppedAtBottom = sentinel != null ||\n"
        "                        (last != null && isListAtBottom(last.offset, last.size, info.viewportEndOffset, nearBottomThresholdPx.toInt()))\n"
        "                }\n"
        "                is androidx.compose.foundation.interaction.DragInteraction.Cancel -> {}\n"
        "                else -> {}\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    clean_transcript = (
        "package x\n"
        "internal fun userRowHasVisibleContent(c: String, i: Collection<*>, a: Collection<*>): Boolean = true\n"
        "internal fun userPartsJsonHasVisibleContent(partsJson: String): Boolean = true\n"
        "fun build() {\n"
        "    if (entity.role == \"user\" && !userRowHasVisibleContent(text, imgs, atts)) return\n"
        "}\n"
    )
    clean_vm = (
        "package x\n"
        "fun retryFromMessage(messageId: String) {\n"
        "    if (!userRowHasVisibleContent(content, imgs, atts)) return\n"
        "    val hasText = userPartsJsonHasVisibleContent(entity.partsJson)\n"
        "}\n"
    )
    clean_queue = (
        "package x\n"
        "fun ChatViewModel.truncateBeforeEdit(messageId: String) {\n"
        "    val hasText = userPartsJsonHasVisibleContent(entity.partsJson)\n"
        "}\n"
    )
    clean_bubble = (
        "package x\n"
        "fun UserMessageBubble(message: ChatMessage) {\n"
        "    if (userRowHasVisibleContent(message.content, message.imageUris, message.attachmentUris)) {\n"
        "        Row {\n"
        "            if (message.content.isNotBlank()) {\n"
        "                Text(message.content)\n"
        "            }\n"
        "            if (isQueued && onWithdraw != null) {\n"
        "                IconButton(onClick = onWithdraw) {\n"
        "                    Icon(contentDescription = stringResource(R.string.chat_withdraw_queued))\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    clean = {
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt": clean_screen,
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatTranscriptRebuild.kt": clean_transcript,
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModel.kt": clean_vm,
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatQueueInterruption.kt": clean_queue,
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatUserMessageUI.kt": clean_bubble,
    }
    got = run_scan(clean)
    results.append(("clean fixture exits clean", got == [], got))

    # N1 (S1): the reversed formula back in the DragStop branch — reds S1 only
    # (S2/S3 stay satisfied).
    n1 = dict(clean)
    n1["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"] = clean_screen.replace(
        "                    val info = listState.layoutInfo\n",
        "                    val stoppedIdx = listState.firstVisibleItemIndex\n"
        "                    val stoppedTotal = listState.layoutInfo.totalItemsCount\n"
        "                    val oldVerdict = stoppedIdx >= stoppedTotal - 2\n"
        "                    val info = listState.layoutInfo\n",
    )
    got = run_scan(n1)
    results.append((
        "N1: reversed formula reds S1 only",
        len(got) == 1 and "[B-03 S1]" in got[0],
        got,
    ))

    # N2 (S2): threshold captured but never consumed — reds S2 only.
    n2 = dict(clean)
    n2["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"] = clean_screen.replace(
        "            isListAtBottom(last, 2, 3, nearBottomThresholdPx.toInt())\n",
        "            isListAtBottom(last, 2, 3, 32)\n",
    )
    got = run_scan(n2)
    results.append((
        "N2: captured-but-unconsumed threshold reds S2 only",
        len(got) == 1 and "[B-03 S2]" in got[0],
        got,
    ))

    # N3 (S3): only one consumer routes through the shared verdict — reds S3 only.
    n3 = dict(clean)
    n3["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"] = clean_screen.replace(
        "                        (last != null && isListAtBottom(last.offset, last.size, info.viewportEndOffset, nearBottomThresholdPx.toInt()))\n",
        "                        (last != null && last.offset + last.size <= info.viewportEndOffset)\n",
    )
    got = run_scan(n3)
    results.append((
        "N3: second caller not routed through the shared verdict reds S3 only",
        len(got) == 1 and "[B-03 S3]" in got[0],
        got,
    ))

    # N4 (V3): withdraw button gated by the caption again — reds V3 only.
    n4 = dict(clean)
    n4["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatUserMessageUI.kt"] = clean_bubble.replace(
        "            if (isQueued && onWithdraw != null) {",
        "            if (message.content.isNotBlank() && isQueued && onWithdraw != null) {",
    )
    got = run_scan(n4)
    results.append((
        "N4: withdraw button re-gated by the caption reds V3 only",
        len(got) == 1 and "[B-05 V3]" in got[0],
        got,
    ))

    # N5 (V2): the transcript call site reverts to the single condition —
    # reds that site's V2 criterion only.
    n5 = dict(clean)
    n5["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatTranscriptRebuild.kt"] = clean_transcript.replace(
        "!userRowHasVisibleContent(text, imgs, atts)) return",
        "text.isBlank() && imgs.isEmpty()) return",
    )
    got = run_scan(n5)
    results.append((
        "N5: transcript call site reverted reds its V2 criterion only",
        len(got) == 1 and "[B-05 V2]" in got[0] and "transcript" in got[0],
        got,
    ))

    # N6 (V1): a duplicate predicate definition — reds V1 only.
    n6 = dict(clean)
    n6["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatTranscriptRebuild.kt"] = clean_transcript.replace(
        "internal fun userRowHasVisibleContent(c: String, i: Collection<*>, a: Collection<*>): Boolean = true\n",
        "internal fun userRowHasVisibleContent(c: String, i: Collection<*>, a: Collection<*>): Boolean = true\n"
        "internal fun userRowHasVisibleContent(c: String, i: Collection<*>, a: Collection<*>): Boolean = false\n",
    )
    got = run_scan(n6)
    results.append((
        "N6: duplicate predicate definition reds V1 only",
        len(got) == 1 and "[B-05 V1]" in got[0],
        got,
    ))

    failed = [name for name, ok, got in results if not ok]
    if failed:
        print("SELF-TEST FAILED (%d breaker(s))" % len(failed))
        for name, ok, got in results:
            if not ok:
                print("  ✗ " + name)
                for f in got:
                    print("      " + f)
        return 1
    print(
        "✅ self-test has teeth: bottom-verdict S1/S2/S3 and visible-content "
        "V1/V2/V3 each flip red on exactly their own breaker; clean fixture green"
    )
    return 0

def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return self_test()
    violations = []
    for sr in SRC_ROOTS:
        for base, _dirs, files in os.walk(os.path.join(root, sr)):
            for fn in sorted(files):
                if not fn.endswith(".kt"):
                    continue
                path = os.path.join(base, fn)
                rel = os.path.relpath(path, os.path.join(root, sr)).replace(os.sep, "/")
                if rel in ALLOWED:
                    continue
                for line_no, pname in scan_file(path, rel):
                    violations.append(f"{rel}:{line_no}  reads plain param `{pname}`")

    # [B-03 deep-audit-1037] bottom-verdict section + [B-05] visible-content
    violations += scan_bottom_verdict(root)
    violations += scan_visible_content(root)

    if violations:
        print(f"❌ Stale-closure guard: {len(violations)} violation(s)")
        print("   A long-lived effect reads a plain parameter / a bottom verdict /")
        print("   a visible-content contract drifted from its single source.")
        print("   Fix: `val latestX by rememberUpdatedState(x)` and read `latestX`,")
        print("   or restore the shared helper the section names.")
        for v in violations:
            print("     " + v)
        return 1
    print("✅ Stale-closure guard: no long-lived effect reads a plain parameter")
    print("✅ Stale-closure guard: bottom verdict + visible-content sections green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
