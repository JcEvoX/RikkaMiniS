# FIX-META-WIDE-shizuku_args — 门见证加固（wide-1024 支 07）

## 1. 门

`scripts/scan/shizuku_args_check.py` — `android-shizuku-cli` 参数处理（16 条判据，`ShizukuOffloadHandler.kt`），基线 `564c8f3e`。

## 2. 轴

| 见证 | 轴 | 判据锚缺陷 |
|---|---|---|
| Criterion 14（netpolicy 方向） | anchor | `src.find` 只锚**首次文本命中**，被死数组吸收 |
| Criterion 13（`--timeout-ms` 剥离） | dataflow | 只验 helper 存在 + splitter 调它，不验 helper 真的剥离 |
| Criterion 15（uid 零回退） | spelling | 只认字面量 `?: "0"`，等价写法全漏 |

## 3. 见证（9 条实锤中本门的 3 条，全部已机械复证）

1. **Criterion 14**：在真正的 `ShizukuManager.runProcess(arrayOf(...))` 上方加一行死数组 `val _unused = arrayOf(... "remove" ... "restrict-background-whitelist" ...)`，再把**实际执行**的数组 `add`/`remove` 写反 → 旧门仍绿。后果：`net restrict-background <pkg> on` 反而把 app 加进免限制白名单，语义完全反转。
2. **Criterion 13**：`stripExecTimeoutTokens` 首行插 `if (true) return tokens to tokens` → 旧门仍绿。后果：`--timeout-ms 5000` 原样进 shell payload，被当成要执行的命令名，所有带该选项的 `shizuku exec` 失败。
3. **Criterion 15**：resolver 尾部 `?: (0).toString()` → 旧门仍绿。后果：未安装包名解析不出 uid 时回落到 UID 0（root），`cmd netpolicy` 打到系统核心 UID。

被挡住（判据承重，未动）：Criterion 3（spelling 轴）。

## 4. 修法

- **C14 → 调用帧绑定**：新增 `_array_span` / `_call_literals`，主体改为「所有命名该 list 且**作为实参传给调用**的 `arrayOf(...)`」；`val x = arrayOf(...)` 的死副本不是执法现场（不计入合格域，也不误判）。判据要求：白名单/黑名单**各至少一个活调用点**，且**每个活调用点**方向正确；否则红（fail-closed 写进 note）。
- **C13 → helper 语义**：新增 `_strips_option_out(body, spellings)`：体内必须（a）出现选项标识（字面量**或**同文件绑定该字面量的常量，如 `EXEC_TIMEOUT_OPT`），（b）有 `while`/`for` 枚举输入，（c）`return` 不得出现在循环之前（早退 = 选项留在 payload），（d）返回的两元组必须是循环填充的那两个累加器且不同一。新增 `_option_spellings` 做常量派生（真树用常量，字面量锚会误红干净树）。
- **C15 → 拼写等价类**：新增 `_zero_fallback`（`?:` 后取表达式、归一化、比对零值拼写集合：`"0"`、`0.toString()`、`(0).toString()`、`Integer.toString(0)`、`String.valueOf(0)`、`"$0"`、`"${0}"` …），作用域限定「两个调用方 + resolver 三个函数体」（文件里别处的 `?: -1` 仍合法）。

新增 selftest 臂 3 条：`14b a dead arrayOf absorbs the anchor and the live call is inverted`、`13d the helper short-circuits and hands the payload back unchanged`、`15d the resolver falls back to a re-spelled root uid`。判据总数 16 不变，breaker 16 + extra 9。

## 5. 四层证据

- L1 真树 `rc=0`（16 判据全 OK）；`--selftest` → `SHIZUKU_ARGS_SELFTEST=PASS criteria=16 breakers=16 extra=9`，收尾标记 `gate flips red on every criterion` 原样保留（`test_scan.py` 全等断言对象）。
- L2 反攻：3/3 CAUGHT（rc 0→1，restore sha256 一致）。
- L3 邻门：`mcp_oauth_lifecycle_check`、`modeluse_proto_single_source_check` 各 rc=0。
- L4 全量：`scan.sh` 112 passed / 0 failed；`test_scan.py` 344 例 0 失败（分批）。

## 6. CI run

分支 `fix/meta-wide-07-mcp-tools`：run `<id>`（回填见最终回报），head_sha 与本地逐一核对。

## 7. 残留

- **C14 只判「作为实参的 arrayOf」**：把两个数组先绑 `val` 再传（`val a = arrayOf(...); runProcess(a)`）会被判"无活调用点"而红。触发条件：真树出现该重构。触发后方案：加一跳 val→call 边回溯。搁置理由：现树是内联实参，回溯会重新打开「死副本吸收锚点」的口子，需要更细的绑定规则。
- **C13 要求 `return` 在循环之后**：`early-return` 型合法实现（先 `if (tokens.isEmpty()) return …` 再循环）会红。触发条件：真树出现该形态。触发后方案：把「早退」判据收窄为「早退返回的正是输入」（当前见证形态）。搁置理由：现树无此形态，放宽会留下 `if (true) return tokens to tokens` 的近邻放行面。
- **C15 零值拼写集合是闭集**：`ZERO_UID` 之类常量回退不在集合内。触发条件：出现常量化的零回退。触发后方案：常量派生（同 `_option_spellings`）。搁置理由：现树无该常量。
- `EXTRA_BREAKERS` 的 `criteria=%d ... extra=%d` 计数行随新增臂变化（16/9）；该行不是 `test_scan.py` 的断言对象（断言的是 `gate flips red on every criterion`）。

## 8. 复现

- 反攻：`python3 /var/minis/shared/work/meta-wide/ops-07/reattack.py`（该门 3 条见证）。
- 单门：`python3 scripts/scan/shizuku_args_check.py .` ／ `--selftest`。
- 脚本：`ops-07/patch_shizuku.py`、`ops-07/freeze_shizuku_arms.py`。
