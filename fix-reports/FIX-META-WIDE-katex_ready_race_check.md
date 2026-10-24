# FIX-META-WIDE-katex_ready_race_check

- 分支：`fix/meta-wide-09-browser-webview`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：组 A（wide-1024 宽扫，25 门/席）
- 生产代码改动：**0 行**（本波零 `.kt` 改动：只改门判据 + 自证臂）
- 冻结面：`scripts/scan/scan.sh` / `scripts/scan/test_scan.py` 未触碰

## 1. 门

`scripts/scan/katex_ready_race_check.py`

锁两个竞态修复的承重形状：`KatexReadyGate` 的「置位 + 取 waiter 一步
原子」「锁外完成」「await 在锁内注册/读取」，以及 `ChatViewModelStore` 的别名读路径
（`resolveKey` 与 `activeSessionId` getter 共用监视器）。本次 A3/A4/B1/B4 四条从
「子串存在」改到「值/归属成立」。

## 2. 轴

| 见证 | 轴 | 新判据 |
|---|---|---|
| A3 `if (!ready) d?.complete(Unit)` | dataflow | 完成调用必须**无条件**执行（允许 `if (d != null)` 等价 null 检查）；被 `!ready` 这类条件门控 = 已在等待的请求全部超时 |
| A4 超时体 await 一个新建 deferred | dataflow | `withTimeoutOrNull(…) { }` 体内 await 的对象必须**是锁内取出的那个本地** |
| B4 getter 里 `resolveKey` 落在 `.also` | dataflow | getter 必须**发布** `resolveKey(...)` 的值（丢弃 ⇒ 草稿键 `__new__…` 原样外泄给 minis-config） |
| B1 `@Synchronized private fun noopLock() {}` 贴着声明 | anchor | 注解必须**贴在本次声明上**：只接受「本行内注解」或「紧邻且内容纯为注解的行」；含其他代码的行终止搜索 |

## 3. 见证（4 条，机械复证结论：打上补丁旧门 rc=0 = 门瞎）

| # | 见证书 | 文件 | 旧判据为何绿 |
|---|---|---|---|
| 1 | `A3 (KatexReadyGate_markPageFinished_atomic)` | `ui/chat/KatexWebViewPool.kt` | 只验证存在 `val d = synchronized(lock) { ready = true; waiter }` 与 `.complete(Unit)` 子串；在完成后加 `if (!ready)` 前缀后两者仍在，而 `ready` 刚被置 true ⇒ 完成永不执行 |
| 2 | `A4 (KatexReadyGate_await_atomic)` | 同上 | 只验证 `withTimeoutOrNull` 与 `.await()` 两个子串；把 `d.await()` 换成 `CompletableDeferred<Unit>().await()` 后子串不变，必然超时 |
| 3 | `B4 (activeSessionId_getter_synchronized)` | `ui/chat/ChatViewModelStore.kt` | 只查 getter 段落里出现 `resolveKey(it)`；`activeSessionIdInternal.also { it?.let { resolveKey(it) } }` 满足子串，返回的却是未解析的键 |
| 4 | `B1 (resolveKey_synchronized)` | 同上 | 只看声明前 3 行窗口里是否含 `@Synchronized`；在上一行插入 `@Synchronized private fun noopLock() {}` 后窗口命中，而真正的 `resolveKey` 无锁 |

## 4. 修法

1. **A3**：从锁块取回捕获的本地名，要求它的 `complete` 调用不被
   `if/else/while/when` 门控；`if (d != null) d.complete(Unit)` 视为等价
   （显式 null 检查），其余条件一律红。
2. **A4**：取锁块产出的本地名，要求 `withTimeoutOrNull(…) { <name>.await() }`。
3. **B4**：在 getter 段落里定位 `resolveKey(`，若落在 `.also`/`.apply` 内即红。
4. **B1**：`annotation_only_lines_above()` —— 向上取行，只接受「内容纯为注解」的行
   （`^@Name(\(…\))?$`），遇到任何其他内容立即停止；另接受本行内注解。

自证臂：W-A3（`!ready` 门控）必红、W-A4（新建 deferred）必红、W-B4（`.also` 丢弃）
必红、W-B1（noopLock 抢占注解）必红，另两条 clean 对照（`if (d != null)` 完成、
`?.let { raw -> resolveKey(raw) }`）必绿。

## 5. 四层证据

### L1 本门

```
python3 scripts/scan/katex_ready_race_check.py .              # rc=0, 真树全绿
python3 scripts/scan/katex_ready_race_check.py . --selftest   # 全臂符合预期（新臂红 / clean 臂绿）
```

既有 8 臂（fixed / A1 / A3 / A4 / B1 / B3 / B4 / C 缺文件）+ **wide-1024 六条**。
收尾标记 `KATEX_READY_RACE_SELFTEST=PASS` 未改动。

### L2 见证反攻（判据链）

L2 用的**不是**我重写的补丁，而是任务书里逐字节的见证原文（脚本：`probe/witness_lib.py`，
按任务书解析 `### 见证` 块 → 打补丁 → 跑门 → 恢复 → sha256 校验）。

```
修复前同一补丁 rc=0（门瞎） → 修复后 rc≠0（门抓住） ⇒ 当初那把刀被挡住了
```

| 见证书 | 轴 | 修复后 rc | restore | 判决 |
|---|---|---|---|---|
| `A3 (KatexReadyGate_markPageFinished_atomic)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `A4 (KatexReadyGate_await_atomic)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `B4 (activeSessionId_getter_synchronized)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `B1 (resolveKey_synchronized)` | anchor | 1 | True | `GATE_CAUGHT_WITNESS` |

判定：**本门 4/4 全抓住**，restore 全 True，工作树零污染（本支 5 门合计 **16/16**，逐条结果见 `probe/l2-*.json`）。

### L3 邻门（各 rc=0）

- `mirror_vm_thread_check.py` → rc=0
- `katex_bridge_arity_check.py` → rc=0

### L4 全量（CI job 日志逐条判读）

- run `40134859565`（#2632）attempt 1 → **success**；job `120994111288`，26 步，
  `not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控，符合预期）
- `head_sha = 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`，与本地 `git rev-parse HEAD` 逐字符一致
- `Summary: 112 passed, 0 failed` 恰一条（合并日志 `0_build.txt` 与
  `build/3_Run pre-build scan gate.txt` 各一次，是同一份输出的两份落盘）
- 门头 `[n/112]`：112 个唯一编号，最大 112（无重号、无缺号）
- `✅ ALL 344 CASES PASS` 恰一条
- Kotlin 编译错误 ` e: `：**0 行**；本仓门输出 `[FAIL]`：**0 行**
- 含 `FAILED` 的 12 行逐条核对：**全部**是 `PASSED` 用例名里的 `FAILED` 子串
  （例：`slot denied produces FAILED without calling provider PASSED`），无一真失败
- 五道门的 `…_SELFTEST=PASS` 标记在 CI 日志中各出现两次（合并日志 + 该步骤单文件），
  说明本轮新增的见证臂在 CI 里真的执行了
- 本支 5 道门在 CI 中的门头：`[62/112] D/ISSUE-04 webview holder url source`、
  `[58/112] G/ISSUE-07 katex ready race + alias lock`、
  `[69/112] C/ISSUE-03 mirror category detail route wiring`、
  `[78/112] C/ISSUE-08 history save snapshot`、
  `[81/112] F/ISSUE-06 mirror vm thread/state`、

## 6. CI run

- run id：`40134859565`
- conclusion：`success`
- head_sha：`5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`（与本地 `git rev-parse HEAD` 逐字符一致）
- 说明：本文件（`fix-reports/`）在绿 sha 之后单独提交，源码树与绿 sha 等价：
  `git diff --name-only 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6..HEAD -- . ':(exclude)fix-reports'` 为空。

## 7. 残留（P2 登记，不修）

1. **A3 的条件识别只到 if/else/while/when** —— 触发条件：把完成包进
   `runCatching { }` 之外的 `when`/`takeIf` 结构；触发后方案：把判据改成「捕获的
   waiter 在函数出口必经路径上被 complete」的支配关系判定；搁置理由：当前形态单一。
2. **A4 只认 `withTimeoutOrNull(…) { }` 体** —— 触发条件：改用
   `withTimeout` / `async { }.await(timeout)`；触发后方案：按「等待锁内 deferred」的
   值流判定，不绑超时 API 名；搁置理由：真树一种写法。
3. **B1 只认 `@Synchronized`** —— 触发条件：改用 `synchronized(this) { }` 包住整个
   `resolveKey` 体；触发后方案：接受块级监视器（并校验读写用同一 monitor）；
   搁置理由：当前实现是注解形态，改成块级会同时打红 B2/B3 的既有臂。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-09-browser-webview
python3 scripts/scan/katex_ready_race_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe/witness_lib.py replay \
    /var/minis/shared/work/meta-wide/task-09-browser-webview.md /var/minis/shared/work/meta-wide/wt-09-browser-webview --gate katex_ready_race_check.py
bash scripts/scan/scan.sh | tail -3
python3 scripts/scan/test_scan.py | tail -2
```
