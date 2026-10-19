#!/usr/bin/env python3
"""
trace_step_tag_check.py — agent 轨迹步骤标注器（分析） + 门禁（CI）

## 这个工具回答什么

「这个 run 到底做了什么」——把已落盘的 trace JSONL 里的每一次工具调用归到
一个语义标签，从而机械回答**「这次 run 有没有做过验证动作」**。此前这个问题
只能靠人读日志。

标签体系取自 bytedance/trae-agent 的 Lakeview（`utils/lake_view.py` 的
`WRITE_TEST / VERIFY_TEST / EXAMINE_CODE / WRITE_FIX / VERIFY_FIX / REPORT /
THINK / OUTLIER`），但打分器换成了**确定性规则**：trae 用 LLM 每步打分，是因为
它手上只有自然语言正文；本仓库的 trace 里有 tool 名 + 完整 args + success +
duration_ms，标签可以直接从结构判定（零调用成本、可复现、可变异验证）。

## 两种用法

    python3 scripts/scan/trace_step_tag_check.py <repo_root>       # 门禁（CI）
    python3 scripts/scan/trace_step_tag_check.py --trace <f.jsonl> # 分析报告
    python3 scripts/scan/trace_step_tag_check.py --selftest        # 牙齿夹具

## 门禁的三面判据（缺一面都会静默失效）

1. **schema 接线** —— 标注器读的是 `AgentTraceRecorder.kt` 写出的字段
   （`type` / `tool` / `args` / `tool_id` / `success`）。若某次重构改了事件名或
   字段名而没人同步这里，标注器不会报错，它只是**安静地数出 0 次调用**，
   然后给出一个看起来很正常的「本次 run 无写动作」结论。所以门必须钉住
   Kotlin 侧的常量与 `put("<字段名>")` 调用仍然在场。
   同一面还钉住**构行的锁归属**（`[da1029-C]`）：`write()` 的行构建必须在
   `synchronized(writeLock)` 内，与 `writeTerminalOnce()` 对称。构行读
   `activeRunId` / `activeSessionId` 并采样 `clock()`，而 `beginRun` 在同一把锁
   内成对更新这两个字段 —— 构行在锁外时，一行可能把 A 的 `run_id` 和 B 的
   `session_id` 拼在一起，且它的 `ts` 会早于并发线程落盘的行（行序与 ts 倒挂）。
2. **真实 golden trace 端到端** —— 用 `tests/traces/golden/traces/*.jsonl`
   （真实事件形状，不是合成夹具）跑一遍，断言每个 `tool_call` 都被标注到、
   标签全在词表内、无解析失败行。
3. **分类契约** —— 一张逐条手写的 `(命令 → 期望标签)` 夹具表，含三条已知的
   误判形状（见下）。期望值是**字面量**，不从被测规则推导——常量改了期望跟着变
   的话，变异会伪装成 PASS。

## 三条已知的误判形状（不是猜测，是实测差分出来的）

| 形状 | 错误做法 | 为什么错 |
|---|---|---|
| 文件名含 `check` 的**读**命令 | `wc -l scripts/scan/trace_eval_check.py` 判成 VERIFY | 判据落在「字符串包含」而不是「动词做了什么」→ 必须先取首动词 |
| `2>/dev/null` | `echo x 2>/dev/null && grep y` 判成 WRITE | stderr 重定向不是产物写入。naive 正则把 `2>` 也算写入，实测让 WRITE 虚高一倍（18 vs 8），**污染下游的验证覆盖率指标** |
| 复合命令只取首个动词 | `cd x && echo … && grep …` 只看 `cd`/`echo` | agent 的 shell 命令绝大多数是 `&&`/`|` 复合串，必须按段取动词再取最信息量的那类 |

## 边界（判据不写清就会狼来了）

- 只对**有写动作的研发类 run** 有意义。一次调研会话里 20 次「写」可能全是
  一次性探索脚本 —— 起手就用同一个指标报警，这个门第三天就会被关掉。
  所以 `--trace` 的裁定会明确区分「无写动作（不适用）」。
- **「跑自写脚本」是定义边界**：`python3 my_probe.py` 无法从脚本名推断用途，
  本工具按「非判据脚本的解释器运行 = EXAMINE」处理。若将来要把它当验证证据，
  必须先扩充词表并明确语义，而不是靠改期望值让夹具变绿。
"""
import glob
import json
import os
import re
import sys

# ── 标签词表 ──────────────────────────────────────────────────────────────
TAGS = {
    "EXAMINE": "读/搜/看 —— 探索代码库或环境",
    "WRITE": "改产物 —— 写文件、改配置、装包、删文件",
    "VERIFY": "跑判据 —— 测试、门禁扫描、编译、哈希对拍、selftest",
    "VCS": "版本控制 —— git / gh 操作",
    "NET": "取外部信息 —— 抓网页、下文件、调外部 API",
    "DELEGATE": "派发 —— 子代理 / 跨会话",
    "OTHER": "其它",
}

# 同一条链上出现多类动作时的优先级：判据 > 版本控制 > 取外部 > 写 > 读。
# 顺序即语义：一条命令跑了测试，那这次调用的价值就是"验证过了"，而不是
# 顺带碰过的文件；同理 clone 比 mkdir 更能说明这次调用在干什么。
PRIORITY = ["VERIFY", "VCS", "NET", "WRITE", "EXAMINE"]

# 命令**首动词** → 类别。判据落在动词上，避免"文件名里含 check 就判 VERIFY"。
TEST_VERBS = {"pytest", "gradlew", "gradle", "junit", "scan.sh", "test_scan.py",
              "sha256sum", "shasum", "md5sum", "kotlinc", "cmp", "tox", "ctest"}
VCS_VERBS = {"git", "gh", "gh_sync.sh", "gh_fullright.sh"}
NET_VERBS = {"curl", "wget", "nc", "openssl"}
WRITE_VERBS = {"mkdir", "mv", "cp", "rm", "chmod", "ln", "touch", "unzip", "zip",
               "tar", "gzip", "apk", "pip", "pip3", "npm", "install"}
READ_VERBS = {"ls", "grep", "rg", "find", "wc", "head", "tail", "file", "which",
              "du", "stat", "tree", "jq", "readlink", "df", "env", "date", "pwd",
              "sort", "uniq", "cut", "diff", "cat", "sed", "awk", "tr", "echo",
              "printf"}
# 这几类"既可能读也可能写"，要再看重定向/`-i` 才能定性。
AMBIGUOUS = {"cat", "sed", "awk", "tr", "echo", "printf", "sort", "uniq", "cut"}
INTERPRETERS = {"python3", "python", "sh", "bash", "zsh", "perl", "ruby", "node"}

# 工具名 → 类别（决定性映射，先于文本规则）。`shell_execute` 不在此表：
# 它的类别必须由命令文本判定。
TOOL_TAGS = {
    "file_read": "EXAMINE",
    "read_image": "EXAMINE",
    "memory_get": "EXAMINE",
    "conversation_history": "EXAMINE",
    "minis_fastio": "EXAMINE",
    "mcp_call": "OTHER",
    "file_write": "WRITE",
    "file_edit": "WRITE",
    "memory_write": "WRITE",
    "memory_rollup": "WRITE",
    "browser_use": "NET",
    "minis_sessions_cli": "DELEGATE",
    "spawn_agent": "DELEGATE",
}

_PREFIX_STRIPPERS = [
    r"timeout\s+\S+\s+",
    r"nice\s+",
    r"cd\s+\S+\s*&&\s*",
    r"[A-Za-z_][A-Za-z0-9_]*=\S*\s+",
    r"nohup\s+",
    r"!?\s*",
]

_VERDICT_SCRIPT = re.compile(
    r"[\w/\-]*(check|test|verify|selftest|scan|probe|mutat|gate)[\w/\-]*\.(py|sh)\b")


def first_verb(cmd):
    """取命令片段的**首个可执行动词**（剥掉 `timeout N` / `cd X &&` / `VAR=val` 等前缀）。"""
    s = (cmd or "").strip()
    for pre in _PREFIX_STRIPPERS:
        s = re.sub(rf"^\s*{pre}", "", s)
    s = s.lstrip("({")  # 子 shell / 分组：`(git clone …)`、`{ echo …; }` 的首动词仍是 git/echo
    head = re.split(r"[;&|]{1,2}|\n", s)[0].strip()
    m = re.match(r"([\w./+\-]+)", head)
    return (m.group(1) if m else "").split("/")[-1]


def parse_args(args_raw):
    """→ (args dict 或空 dict, 尽力还原的命令文本)。

    **长命令的 args 是坏的 JSON**，这不是理论风险：`AgentTraceRecorder` 把 args
    截到 `ARGS_MAX_LENGTH = 500`（见 `put("args", sanitize(argsJson, 500))`），
    超过 500 字符的命令会被截在字符串中间 → `json.loads` 必然失败。
    此时若直接放弃，**恰恰是最长、最复杂的那些命令会一律落到 OTHER**，
    而它们正是最需要被标注的调用。所以按 JSON 前缀尽力抽取。
    """
    try:
        parsed = json.loads(args_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed, str(parsed.get("command", "")) or str(parsed.get("cmd", ""))

    m = re.search(r'"(?:command|cmd)"\s*:\s*"(.*)', args_raw or "", re.S)
    if not m:
        return {}, ""
    frag = m.group(1)
    frag = re.sub(r"\\u[0-9a-fA-F]{4}", " ", frag)   # 截断处的半个转义也别让它污染动词
    for esc, lit in (("\\/", "/"), ('\\"', '"'), ("\\n", "\n"), ("\\t", " ")):
        frag = frag.replace(esc, lit)
    return {}, frag


def has_real_redirect(seg):
    """真写入重定向。排除 `2>` / `2>&1`（只重定向 stderr）与 `>/dev/null`。

    这条是实测出来的：naive 版本把 `2>/dev/null` 当写入，让 WRITE 虚高一倍，
    而 WRITE 虚高会直接污染下游「验证覆盖率」的分子/分母。
    """
    for m in re.finditer(r"(?<![0-9])>>?\s*(\S+)", seg):
        if m.group(1) != "/dev/null":
            return True
    return False


def classify_segment(seg):
    """把一个命令片段归到某一个类别；无法判定返回 None。"""
    v = first_verb(seg)
    if not v:
        return None
    if v in TEST_VERBS:
        return "VERIFY"
    if v in VCS_VERBS:
        return "VCS"
    if v in NET_VERBS:
        return "NET"
    if v in INTERPRETERS:
        # 解释器跑的是哪个脚本，决定这次调用在干什么
        if re.search(r"(^|[\s/])(gh_sync|gh_fullright)\.sh\b", seg):
            return "VCS"
        if _VERDICT_SCRIPT.search(seg):
            return "VERIFY"
        # 跑判据脚本 = 验证；跑一次性脚本 = 探索（用途不可从名字推断，见模块 docstring 边界）
        return "WRITE" if has_real_redirect(seg) else "EXAMINE"
    if v in AMBIGUOUS:
        if re.search(r"sed\s+-i", seg) or has_real_redirect(seg) or re.search(r"\|\s*tee\s", seg):
            return "WRITE"
        return "EXAMINE"
    if v in WRITE_VERBS:
        return "WRITE"
    if v in READ_VERBS:
        return "EXAMINE"
    return None


def tag_call(tool, args_raw):
    """给一次工具调用打标签。`args_raw` 是 trace 里的原始 args 字符串。"""
    t = (tool or "").strip()
    a, cmd = parse_args(args_raw)

    if t not in ("shell_execute", "bash", "terminal"):
        if t in TOOL_TAGS:
            base = TOOL_TAGS[t]
            if base == "EXAMINE":
                # 读类工具也可能藏着外部抓取（如 browser_use 的 url）
                blob = " ".join(str(a.get(k, "")) for k in ("url", "query", "pattern"))
                if re.search(r"https?://", blob):
                    return "NET"
            return base
        return "OTHER"

    if not cmd:
        return "OTHER"

    found = set()
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        c = classify_segment(seg.strip())
        if c:
            found.add(c)
    for cls in PRIORITY:
        if cls in found:
            return cls
    return "OTHER"


# ── trace 读取与分析 ─────────────────────────────────────────────────────
TRACE_REQUIRED_FIELDS = ["tool", "args", "tool_id", "success"]
TRACE_REQUIRED_TYPES = ["tool_call", "tool_result", "turn_start", "turn_end"]


def load_events(path):
    """读 JSONL；坏行跳过并计数（trace 是旁路证据，局部写坏不该让整个分析崩掉）。"""
    events, bad = [], 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                events.append(obj)
            else:
                bad += 1
    return events, bad


def analyze(events):
    """把事件流还原成 (turns, 标注序列, 按标签计数, 按标签失败计数)。"""
    calls, order, results = {}, [], []
    for e in events:
        ty = e.get("type")
        if ty == "tool_call":
            calls[str(e.get("tool_id"))] = e
            order.append(str(e.get("tool_id")))
        elif ty == "tool_result":
            results.append(e)

    seq = []
    for tid in order:
        c = calls.get(tid, {})
        raw = c.get("args") or ""
        seq.append({
            "tool_id": tid,
            "turn": c.get("turn"),
            "tool": c.get("tool"),
            "tag": tag_call(c.get("tool"), raw),
            "args": raw[:180],
        })

    counts, fails = {}, {}
    for s in seq:
        counts[s["tag"]] = counts.get(s["tag"], 0) + 1
    for r in results:
        c = calls.get(str(r.get("tool_id")), {})
        if not r.get("success", True):
            t = tag_call(c.get("tool"), c.get("args") or "")
            fails[t] = fails.get(t, 0) + 1

    turns = sum(1 for e in events if e.get("type") == "turn_end")
    return {"turns": turns, "calls": len(seq), "counts": counts, "fails": fails, "seq": seq}


def verdict(counts):
    """验证覆盖率裁定。scope 说明见模块 docstring 的"边界"一节。"""
    ver, wri = counts.get("VERIFY", 0), counts.get("WRITE", 0)
    if wri == 0:
        return "无写动作 —— 纯调研/问答 run，验证覆盖率不适用。"
    if ver == 0:
        return "★ 有 %d 次写动作但 0 次验证 —— 产出未经任何判据检验。" % wri
    return "写 %d 次 / 验证 %d 次 = 每 %.2f 次写有 1 次验证。" % (wri, ver, wri / ver)


def report(path):
    events, bad = load_events(path)
    a = analyze(events)
    print("trace: %s" % path)
    print("事件 %d 条（坏行 %d）· %d 轮 · %d 次工具调用"
          % (len(events), bad, a["turns"], a["calls"]))
    print("-" * 68)
    total = a["calls"] or 1
    for tag in list(TAGS) + ["OTHER"]:
        n = a["counts"].get(tag, 0)
        if not n:
            continue
        f = a["fails"].get(tag, 0)
        print("%-9s %3d  %-30s %3d%%  %s%s"
              % (tag, n, "#" * max(1, round(n * 30 / total)), n * 100 // total,
                 TAGS.get(tag, ""), ("  失败 %d" % f) if f else ""))
    print("-" * 68)
    print("裁定：" + verdict(a["counts"]))
    return 0


# ── 门禁 ────────────────────────────────────────────────────────────────
# 逐条手写的分类契约。期望值是字面量 —— 不许从 tag_call 推导。
# 覆盖：三类基础动作 × 三条已知误判形状 × 边界形态（无重定向 / 链式 / 解释器）。
CONTRACT = [
    # 读
    ("shell_execute", '{"command":"ls -la /tmp"}', "EXAMINE"),
    ("shell_execute", '{"command":"grep -rn \\"foo\\" src/"}', "EXAMINE"),
    ("shell_execute", '{"command":"cd /x && grep -rn foo src/"}', "EXAMINE"),
    ("shell_execute", '{"command":"sed -n \'1,20p\' a.kt"}', "EXAMINE"),
    ("shell_execute", '{"command":"find src -name \'*.kt\' | xargs wc -l"}', "EXAMINE"),
    # 形状①：文件名含 check 的**读**命令 —— 判据必须落在动词上
    ("shell_execute", '{"command":"cd /r && wc -l scripts/scan/trace_eval_check.py"}', "EXAMINE"),
    ("shell_execute", '{"command":"cat scripts/scan/test_scan.py"}', "EXAMINE"),
    # 形状②：`2>/dev/null` 不是写入；naive 正则在此让 WRITE 虚高一倍
    ("shell_execute", '{"command":"echo \'=== x ===\' 2>/dev/null && grep -n y z.kt"}', "EXAMINE"),
    ("shell_execute", '{"command":"du -sh . 2>/dev/null"}', "EXAMINE"),
    ("shell_execute", '{"command":"minis-model-use list 2>&1 | head -60"}', "EXAMINE"),
    # 形状② 的另一半：`2>&1` 也不是写入。naive 正则在此匹配到 `>&1`（第 0 组 = `&1`），
    # 与 /dev/null 排除无关，所以只有 lookbehind 能挡 —— 单独一臂钉住它。
    ("shell_execute", '{"command":"echo \'=== x ===\' 2>&1 | head -5"}', "EXAMINE"),
    # 形状② 的第三种写法：**裸** `> /dev/null`（无 fd 数字，lookbehind 挡不住）。
    # 仓库约定里的后台服务写法就是 `cmd > /dev/null 2>&1 &`，这是真实高频形状。
    ("shell_execute", '{"command":"echo done > /dev/null && grep -n x y.kt"}', "EXAMINE"),
    # 形状③：链式命令 —— 全链的类别由最信息量的那类决定，不是首动词
    ("shell_execute", '{"command":"cd /w && mkdir -p a && cd a && git clone --depth 1 u r"}', "VCS"),
    ("shell_execute", '{"command":"cd /w && rm -f o.txt && python3 gate_check.py"}', "VERIFY"),
    ("shell_execute", '{"command":"curl -s http://x/api | head -5"}', "NET"),
    # 写
    ("shell_execute", '{"command":"echo hi > /tmp/x"}', "WRITE"),
    ("shell_execute", '{"command":"sed -i \'s/a/b/\' f.kt"}', "WRITE"),
    ("shell_execute", '{"command":"mkdir -p /var/minis/shared/work/x"}', "WRITE"),
    ("shell_execute", '{"command":"cat > /var/minis/workspace/f.py"}', "WRITE"),
    ("shell_execute", '{"command":"git apply p.diff"}', "VCS"),
    # 验证
    ("shell_execute", '{"command":"./gradlew testReleaseUnitTest"}', "VERIFY"),
    ("shell_execute", '{"command":"pytest -q tests/"}', "VERIFY"),
    ("shell_execute", '{"command":"sh scripts/scan/scan.sh"}', "VERIFY"),
    ("shell_execute", '{"command":"sha256sum -c sums.txt"}', "VERIFY"),
    ("shell_execute", '{"command":"cd /r && python3 scripts/scan/x_check.py --selftest"}', "VERIFY"),
    # VCS / NET
    ("shell_execute", '{"command":"git log --oneline -3"}', "VCS"),
    ("shell_execute", '{"command":"sh gh_sync.sh push --branch b"}', "VCS"),
    ("shell_execute", '{"command":"wget -q https://x/y.zip"}', "NET"),
    # 边界：非 shell 工具走决定性映射
    ("file_read", '{"path":"/tmp/a.kt"}', "EXAMINE"),
    ("file_write", '{"path":"/tmp/a.kt","content":"x"}', "WRITE"),
    ("spawn_agent", '{"query":"do it"}', "DELEGATE"),
    ("browser_use", '{"action":"navigate","url":"https://x.com"}', "NET"),
    # 边界：无命令的 shell 调用不得崩
    ("shell_execute", "{}", "OTHER"),
    # 边界：子 shell / 分组前缀 —— `(git …)` 的首动词仍是 git，不是 '('
    ("shell_execute",
     '{"command":"cd /a && mkdir -p w && (git clone --depth 1 u r 2>&1 | tail -5) && du -sh r"}',
     "VCS"),
    # 形状④：args 被 AgentTraceRecorder 截到 500 字符 → **坏 JSON**。
    # 长命令恰恰最需要被标注，不能因为解析失败就一律落到 OTHER。
    ("shell_execute",
     '{"command":"cd \\/a && echo \\"=== x ===\\" && grep -rn \\"StepKind\\\\|stepType\\" src\\/tools\\/*.kt src',
     "EXAMINE"),
]


def face_contract():
    """面 3：分类契约逐条比对。"""
    bad = []
    for tool, args, expect in CONTRACT:
        got = tag_call(tool, args)
        if got != expect:
            bad.append("    %s %s -> 期望 %s 实得 %s" % (tool, args[:64], expect, got))
    if bad:
        print("  ❌ 分类契约失配 %d 条：" % len(bad))
        for b in bad:
            print(b)
        return False
    print("  ✅ 分类契约 %d 条全对" % len(CONTRACT))
    return True


def face_golden(root):
    """面 2：真实 golden trace 端到端。标签数必须与 tool_call 数一一对上。"""
    traces = sorted(glob.glob(os.path.join(
        root, "tests", "traces", "golden", "traces", "*.jsonl")))
    if not traces:
        print("  ❌ 找不到 tests/traces/golden/traces/*.jsonl —— 端到端面无法取证")
        return False
    ok = True
    for path in traces:
        events, bad = load_events(path)
        a = analyze(events)
        declared = sum(1 for e in events if e.get("type") == "tool_call")
        problems = []
        if bad:
            problems.append("坏行 %d" % bad)
        if declared == 0:
            problems.append("0 次 tool_call（真实 golden 不该为空）")
        if a["calls"] != declared:
            problems.append("标注 %d != 声明 %d" % (a["calls"], declared))
        unknown = [t for t in a["counts"] if t not in TAGS and t != "OTHER"]
        if unknown:
            problems.append("标签越界 %s" % unknown)
        if problems:
            print("  ❌ %s：%s" % (os.path.basename(path), "; ".join(problems)))
            ok = False
        else:
            print("  ✅ %s：%d 次调用 -> %s"
                  % (os.path.basename(path), a["calls"],
                     ", ".join("%s=%d" % (k, a["counts"][k]) for k in sorted(a["counts"]))))
    return ok


def strip_literals(src):
    """Empty out string bodies + drop comments before any brace matching.

    The KDoc of `write` quotes `synchronized(writeLock)` / `buildLine` verbatim,
    so a comment-blind matcher could be satisfied by documentation alone.
    Returns None when a literal/comment never terminates (caller reports ROT
    instead of matching garbage).
    """
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith('"""', i):
            end = src.find('"""', i + 3)
            if end < 0:
                return None
            out.append("~")
            i = end + 3
            continue
        c = src[i]
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            if i >= n:
                return None
            out.append("~")
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def brace_block_end(code, open_idx):
    """Index just past the '}' matching the '{' at `open_idx`, or -1."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


# 面 1b 的锚点（[da1029-C / I09]）
WRITE_DECL = "private fun write("
TERMINAL_DECL = "private fun writeTerminalOnce("
LOCK_CALL = "synchronized(writeLock)"
BUILD_LINE_CALL = "buildLine("


def face_lock_scope(src):
    """面 1b [da1029-C / I09]：构行必须在 writeLock 内 —— 与 writeTerminalOnce 对称。

    `buildLine` 读非 @Volatile 的 activeRunId / activeSessionId 并采样 clock()，
    而 `beginRun` 是在同一把锁内**成对**更新这两个字段的。构行放在锁外时：
    本行可能读到半更新的 run 上下文（run_id 来自 A、session_id 来自 B），且
    采样到的 `ts` 会早于并发线程落盘的行 —— 落盘行序与 ts 倒挂。

    判据落在**锁归属**上（不是 buildLine 的调用形态），所以把构行内联进
    synchronized 块、或先赋给局部变量，都算通过。
    """
    problems = []
    code = strip_literals(src)
    if code is None:
        return ["AgentTraceRecorder.kt: unterminated string/comment literal — cannot "
                "brace-match write() (ROT — update this gate)"]
    for decl in (WRITE_DECL, TERMINAL_DECL):
        start = code.find(decl)
        if start < 0:
            problems.append("AgentTraceRecorder.kt: '%s' not found (ROT — update this gate)" % decl)
            continue
        open_brace = code.find("{", start)
        end = brace_block_end(code, open_brace) if open_brace >= 0 else -1
        if end < 0:
            problems.append("AgentTraceRecorder.kt: %s body braces do not balance "
                            "(ROT — update this gate)" % decl)
            continue
        body = code[open_brace:end + 1]
        name = decl[len("private fun "):-1]
        lock_at = body.find(LOCK_CALL)
        if lock_at < 0:
            problems.append("AgentTraceRecorder.kt: %s has no '%s' around the line build — "
                            "the run context it reads is written under that lock by beginRun"
                            % (name, LOCK_CALL))
            continue
        lock_open = body.find("{", lock_at)
        lock_end = brace_block_end(body, lock_open) if lock_open >= 0 else -1
        if lock_end < 0:
            problems.append("AgentTraceRecorder.kt: %s's synchronized block braces do not "
                            "balance (ROT — update this gate)" % name)
            continue
        builds = [m.start() for m in re.finditer(re.escape(BUILD_LINE_CALL), body)]
        if not builds:
            problems.append("AgentTraceRecorder.kt: %s never calls '%s' — the write path "
                            "cannot be serialized (ROT — update this gate)"
                            % (name, BUILD_LINE_CALL))
            continue
        for p in builds:
            if not (lock_open < p < lock_end):
                problems.append("AgentTraceRecorder.kt: '%s' at body offset %d of %s is outside "
                                "'%s' — a line built while the lock is free can pair run_id from "
                                "one run with session_id from the next, and its ts can precede "
                                "lines appended by a concurrent writer"
                                % (BUILD_LINE_CALL, p, name, LOCK_CALL))
    return problems


def face_schema(root):
    """面 1：schema 接线 —— Kotlin 侧仍写出标注器依赖的事件名与字段名 + 构行在锁内。"""
    kt = os.path.join(root, "src", "android", "app", "src", "main", "java",
                      "com", "rikkaminis", "app", "tools", "AgentTraceRecorder.kt")
    if not os.path.exists(kt):
        print("  ❌ 找不到 AgentTraceRecorder.kt —— 接线面无法取证")
        return False
    src = open(kt, "r", encoding="utf-8", errors="replace").read()
    missing = []
    for ty in TRACE_REQUIRED_TYPES:
        if ('"%s"' % ty) not in src:
            missing.append("事件类型 %s" % ty)
    for fld in TRACE_REQUIRED_FIELDS:
        if ('put("%s"' % fld) not in src:
            missing.append("字段 put(\"%s\")" % fld)
    if missing:
        print("  ❌ 标注器依赖的 trace 契约在 Kotlin 侧缺失：%s" % ", ".join(missing))
        print("     （标注器不会报错，只会安静地数出 0 次调用 —— 本面就是拦这个）")
        return False
    print("  ✅ AgentTraceRecorder.kt 仍写出 %d 个事件类型 + %d 个字段"
          % (len(TRACE_REQUIRED_TYPES), len(TRACE_REQUIRED_FIELDS)))
    lock_problems = face_lock_scope(src)
    if lock_problems:
        for p in lock_problems:
            print("  ❌ " + p)
        return False
    print("  ✅ write()/writeTerminalOnce() 的行构建都在 %s 内（锁归属判据）" % LOCK_CALL)
    return True


def gate(root):
    print("trace_step_tag_check — 标注器三面判据")
    results = [face_schema(root), face_golden(root), face_contract()]
    if all(results):
        print("TRACE_STEP_TAG_CHECK: OK")
        return 0
    print("TRACE_STEP_TAG_CHECK: FAIL")
    return 1


# ── selftest：证明三面各自有牙 ───────────────────────────────────────────
def selftest():
    """三面各造一个坏输入，断言对应面**当场变红**且互不连坐。

    没有这一段，"门被掏空成 print(OK)" 与"门真的在判"在输出上无法区分。
    """
    global CONTRACT
    import contextlib
    import io
    import shutil
    import tempfile

    fails = []

    def quiet(fn, *a):
        """吞掉被测面的输出（它按设计会打印红色诊断），只取返回值。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return fn(*a)

    # 牙 1：Kotlin 侧少一个字段 -> 面 1 必须红
    root = tempfile.mkdtemp(prefix="tst-tag-")
    kt_dir = os.path.join(root, "src", "android", "app", "src", "main", "java",
                          "com", "rikkaminis", "app", "tools")
    os.makedirs(kt_dir)
    with open(os.path.join(kt_dir, "AgentTraceRecorder.kt"), "w") as fh:
        fh.write('const val TYPE_TOOL_CALL = "tool_call"\n'
                 'const val TYPE_TOOL_RESULT = "tool_result"\n'
                 'const val TYPE_TURN_START = "turn_start"\n'
                 'const val TYPE_TURN_END = "turn_end"\n'
                 'put("tool", x)\nput("tool_id", x)\nput("success", x)\n')
    # 注意：此处**故意**不写 put("args", …) —— 少一个字段就是面 1 的坏输入
    if quiet(face_schema, root):
        fails.append("牙1：Kotlin 缺 put(\"args\") 时面 1 竟然还是绿的")
    elif quiet(gate, root) == 0:
        fails.append("牙1：门整体在坏 Kotlin 上返回 0")
    shutil.rmtree(root, ignore_errors=True)

    # 牙 2：golden trace 被换成空 -> 面 2 必须红
    root = tempfile.mkdtemp(prefix="tst-tag-")
    gdir = os.path.join(root, "tests", "traces", "golden", "traces")
    os.makedirs(gdir)
    with open(os.path.join(gdir, "empty.jsonl"), "w") as fh:
        fh.write('{"type":"trace_start"}\n')
    if quiet(face_golden, root):
        fails.append("牙2：只有 trace_start 的 golden 竟然通过了面 2（0 次调用未被拦）")
    shutil.rmtree(root, ignore_errors=True)

    # 牙 3：把契约里一条期望值翻反 -> 面 3 必须红（证明契约真的在比对）
    keep = list(CONTRACT)
    CONTRACT = keep + [("shell_execute", '{"command":"pytest -q"}', "EXAMINE")]
    if quiet(face_contract):
        fails.append("牙3：期望值被翻反后契约仍然全对 —— 契约没在比对")
    CONTRACT = keep

    # 精度对照：契约在**正确**期望下必须绿（防"全红也算有牙"）
    if not quiet(face_contract):
        fails.append("精度对照：正确期望下契约竟然红了")

    # 牙 4 [da1029-C / I09]：把 write() 的 buildLine 挪到 writeLock 外 -> 接线面红
    def _kt(lock_scope_write):
        head = ('const val TYPE_TOOL_CALL = "tool_call"\n'
                'const val TYPE_TOOL_RESULT = "tool_result"\n'
                'const val TYPE_TURN_START = "turn_start"\n'
                'const val TYPE_TURN_END = "turn_end"\n'
                'put("tool", x)\nput("args", x)\nput("tool_id", x)\nput("success", x)\n')
        sig = '    private fun write(type: String, block: JSONObject.() -> Unit) {\n'
        build = '        val line = buildLine(type, block)\n'
        lock_open = '        synchronized(writeLock) {\n'
        append = '            runCatching { appendLine(line) }\n'
        close = '        }\n    }\n'
        if lock_scope_write:
            write_fn = sig + lock_open + build + append + close
        else:
            write_fn = sig + build + lock_open + append + close
        terminal = ('    private fun writeTerminalOnce(block: JSONObject.() -> Unit) {\n'
                    + lock_open + build + append + close)
        return head + write_fn + terminal

    root = tempfile.mkdtemp(prefix="tst-tag-")
    kt_dir = os.path.join(root, "src", "android", "app", "src", "main", "java",
                          "com", "rikkaminis", "app", "tools")
    os.makedirs(kt_dir)
    path = os.path.join(kt_dir, "AgentTraceRecorder.kt")
    with open(path, "w") as fh:
        fh.write(_kt(lock_scope_write=False))
    if quiet(face_schema, root):
        fails.append("牙4：buildLine 被挪到 writeLock 外时，接线面竟然还是绿的")
    with open(path, "w") as fh:
        fh.write(_kt(lock_scope_write=True))
    if not quiet(face_schema, root):
        fails.append("牙4 精度对照：构行在锁内时接线面竟然红了")
    shutil.rmtree(root, ignore_errors=True)

    if fails:
        for f in fails:
            print("  ❌ " + f)
        print("TRACE_STEP_TAG_SELFTEST=FAIL")
        return 1
    print("  牙1 Kotlin 缺字段 -> 接线面红 / 牙2 空 golden -> 端到端面红 / "
          "牙3 期望翻反 -> 契约面红 / 牙4 buildLine 出锁 -> 接线面红；"
          "三者互不连坐，正确输入下全绿")
    print("TRACE_STEP_TAG_SELFTEST=PASS")
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--trace" in argv:
        i = argv.index("--trace")
        if i + 1 >= len(argv):
            print("--trace 需要一个 JSONL 路径")
            return 2
        return report(argv[i + 1])
    root = None
    for a in argv[1:]:
        if not a.startswith("-"):
            root = a
            break
    if root is None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if not os.path.isdir(root):
        print("repo root 不存在：%s" % root)
        return 2
    return gate(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
