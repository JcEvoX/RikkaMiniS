# FIX-META-WIDE-provider_rpc_symmetry_check

- 分支：`fix/meta-wide-02-provider-symmetry`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 波次：meta-audit-witness / wide-1024（组 B 报告）
- 生产代码改动：**0 行**（只改门判据 + 门自证臂）

## 1. 门

`scripts/scan/provider_rpc_symmetry_check.py` — 19 条判据（C1..C19），本次加固 3 条：
**C4**（collection 面 customBaseURL 守卫）、**C6**（azureMode 两口写faces）、**C19**（session 配置读穿到驻留 VM）。

## 2. 轴

三条全是 **dataflow**：判据都停在「某个 token / 某个调用存在」，没有跟到「这个值决定了结果」。
- C4 存在性 → 守卫必须**决定分支且让分支终结**（或守卫的返回值本身就是被写的值）。
- C6 存在性 → 解析结果必须**流入**存储值（直接、局部别名、一跳 helper）。
- C19 存在性（`"(" in arm`）→ 分支给出的值必须**来自**驻留 VM 的 peek。

## 3. 见证（3 条，均已机械复证）

| # | 判据 | 见证（见证原文的 find → replace） | 修复前 | 修复后 | 恢复 |
|---|---|---|---|---|---|
| 1 | C4 | `if (!isCustomBaseURLWriteAllowed(inst.providerType, newBase))` → `if (false && !isCustomBaseURLWriteAllowed(inst.providerType, newBase))` | rc=0（门瞎） | rc=1 | sha256 一致 |
| 2 | C19 | 驻留分支 `vm!!.requestedThinkingLevel.value` → `(ThinkingLevel.OFF)` | rc=0（门瞎） | rc=1 | sha256 一致 |
| 3 | C6 | `val azureMode = params.optBoolean("azureMode", false)` → `val _parsedAzure = …` + `val azureMode = false` | rc=0（门瞎） | rc=1 | sha256 一致 |

反攻脚本：`/tmp/verify_rpc.py`（对每条：原文计数必须恰 1 → 打补丁 → 跑旧门（`git show HEAD:` 版）与当前门 → `git checkout --` 恢复 → 比 sha256）。
输出：

```
C4-false-and       old_rc=0 new_rc=1 restore=True
C19-paren-constant old_rc=0 new_rc=1 restore=True
C6-parked-parse    old_rc=0 new_rc=1 restore=True
```

**守卫不动**：任务书点名「攻击被挡住、不要动也不要删」的 C9 形状（把共享守卫换成内联比较并让写入脱离 throw）复测 —— `rc=1`，restore=True。

## 4. 修法

新增/改写（均在门文件内，无生产改动）：

- `_strip_parens` / `_split_top(op)` / `_constant_condition(cond)`：**布尔常量求值**。先剥冗余括号，再按 Kotlin 优先级在 depth 0 切 `||`（任一析取项为字面 `true` → 条件恒真），每个析取项切 `&&`（任一合取项为字面 `false` → 该析取恒假）；字面量短路 ⇒ 守卫不再是守卫。字符串字面量对切分不透明（`"a && b"` 不会误切）。
- `_bound_initialisers` / `_decl_text`：`val|var id[: T] = …` 的右值索引 + **表达式体声明**文本（`fun f() = …`；`extract_function` 只认 `{}` 体，会把表达式体 helper 判成「不存在」）。
- `_guard_branches(body, aliases)`：枚举所有 `if (…)`，条件里含守卫 token **或**含 `val ok = <守卫>(…)` 的本地别名（合法重构不能判红）。
- C4 判定改为：(a) 存在**活且终结**的守卫（条件未被字面量短路 + 分支内 `throw`/`return`）；或 (b) 被写的值**派生自**守卫调用（`val x = resolveAddCustomBaseURL(raw)` → `x` 被写）。两条都不成立才红；红时分三种消息（被字面量短路 / 守卫不终结 / 根本没有守卫）。
- C6 判定：`_azure_value_flows` —— 从 `azureMode` 的绑定右值出发做 BFS（局部别名 + 一跳 helper，helper 用 `_decl_text` 解析），必须触达 `optBoolean("azureMode"`。解析结果停在无用变量、`azureMode` 被赋常量 ⇒ 红。两侧（create / update）各一条。
- C19 判定：`_vm_handles` 从块内 `val vm = <peek>(…)` 派生「驻留 VM 句柄」标识符（callee 为 `peekViewModel` 或体内触达它的一跳 helper），要求分支表达式的值**消费该句柄**；只有在块内没有句柄（内联 `peekViewModel(…)?.let { }` 形态）时才回退到「分支文本本身含 peek」。删掉了 `"(" in arm` 这个「有括号 ⇒ 是调用 ⇒ 是读」的代偿判据。

**自证臂**（SELFTEST 收尾标记一字未改）：

| 臂 | 断言 | 形态 |
|---|---|---|
| F61-collection-guard-short-circuited-by-false-C4 | 恰好 C4 红 | 见证 1 |
| F62-collection-guard-via-local-alias-green | 绿 | 接受臂：`val ok = isAllowed(…)` + `if (!ok)` 是合法重构 |
| F63-session-think-reader-parenthesised-constant-C19 | 恰好 C19 红 | 见证 2 |
| F64-create-parks-parse-then-constant-C6 | 恰好 C6 红 | 见证 3（create 面） |
| F65-update-parks-parse-then-constant-C6 | 恰好 C6 红 | 见证 3 的 update 面（同一口写 faces 的另一半） |
| F66-create-azureMode-via-local-alias-green | 绿 | 接受臂：`val rawAzure = optBoolean(…)` + `val azureMode = rawAzure` |

## 5. 四层证据

- **L1 本门（真树）**：`python3 scripts/scan/provider_rpc_symmetry_check.py .` → `ALL GREEN (C1..C19)` rc=0；`--selftest` → 全部 PASS + `PROVIDER_RPC_SELFTEST=PASS`。
- **L2 见证反攻**：上表 3/3 `old_rc=0 → new_rc=1`，restore=True；C9 守卫形状仍 rc=1。
- **L3 邻门**：`provider_ua_parity_check` rc=0、`provider_models_cache_wiring_check` rc=0。
- **L4 全量**：`scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`；`scripts/scan/test_scan.py` → 分支 CI **ALL 344 CASES PASS**（见 §6）；本机两个相关夹具组 `test_da1019_gates` / `test_da1025_gates` 复跑全 ✅。门数/夹具数不变：112 门 / 344 夹具。

## 6. CI run

- 分支 CI（手动 dispatch，`on: push` 只认 main）：**run `40136767900` / #2640 / job `120999811778`**，`conclusion=success`，attempt 1。
- 三方 sha 逐字符一致：本地 HEAD = 远端分支 ref = `run.head_sha` = `02aad4c5326eae83afab27ad477edab71039e044`。
- 26 步 `not_ok=[]`，唯一 `skipped` = **Publish to Releases**（`if: github.ref == 'refs/heads/main'` 门控，分支预期）。
- 日志判读（1,484,497 B）：
  - `Summary: 112 passed, 0 failed` 恰 **1** 条（该后缀无其它命中）；
  - `ALL 344 CASES PASS` 恰 **1** 条；
  - ` e: ` 行 **0** 条（无编译错误回显）；
  - `PROVIDER_RPC_SELFTEST=PASS` / `PROVIDER_UA_PARITY_SELFTEST=PASS` 各 1 条（`✅ … selftest has teeth`），即新增臂在 CI 里被 `test_scan.py` 断言见证（门 selftest 逐臂 stdout 不进 CI 日志，标记行是权威见证）；
  - 含 `FAILED` 的行 6 条，逐条确认**全是 PASSED 用例名**（如 `slot denied produces FAILED without calling provider PASSED`），游离失败 0。

## 7. 残留（均带三件）

1. **C4 · 别名洗白**：`val ok = false && isAllowed(…)` + `if (!ok) throw` —— 别名条件被判为「活」，守卫实际恒假。
   - 触发条件：有人把字面量短路搬进别名初始化器（比直接写在 `if` 里多一步）。
   - 触发后方案：`aliases` 收进判定时先对右值跑 `_constant_condition`；右值为恒假则别名不计入「承重守卫」。
   - 搁置理由：当前真树与见证都不走这条路径；再加一层会让判据更绕，等真出现再加。
2. **C6 · 常量选支**：`val azureMode = if (false) params.optBoolean("azureMode", false) else false` —— 右值含解析 ⇒ 判绿。
   - 触发条件：有人用常量条件把解析分支停用。
   - 触发后方案：把 `_constant_condition` 用在 `if/else` 表达式上，从存活分支取右值再判 dataflow。
   - 搁置理由：与 §2 第 1 条同族、同层；两条一起做才有意义。
3. **C19 · 句柄在手但值仍是常量**：`vm!!.requestedThinkingLevel.let { (ThinkingLevel.OFF) }` —— 消费了句柄，值仍是常量。
   - 触发条件：分支在消费句柄后又丢弃它。
   - 触发后方案：判据从「分支引用句柄」升级为「分支的**返回表达式**派生自句柄」（需要最小表达式求值，`let`/`?:` 形态各自处理）。
   - 搁置理由：本波要求「抓住那把刀、不误伤真树」，这条属于更深的语义分析，装置比例不符（与修复门纪律一致）。
4. **`_decl_text` 截断启发式**：一跳 helper 的文本取到「下一个顶层声明」为止，若 helper 定义在密集注解之间可能截断不准。
   - 触发条件：helper 出现在带注解的顶层声明群中。
   - 触发后方案：改用已有的 `TOP_DECL_PAT` 做唯一切断依据。
   - 搁置理由：真树与臂都不触发；目前 fail-closed 方向安全（截断后找不到 parse 会判红，不会静默放行）。

## 8. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-02-provider-symmetry

# L1
python3 scripts/scan/provider_rpc_symmetry_check.py .
python3 scripts/scan/provider_rpc_symmetry_check.py --selftest

# L2（三条见证，含旧门对照与 sha256 恢复校验）
git show HEAD:scripts/scan/provider_rpc_symmetry_check.py > /tmp/old_rpc_gate.py
python3 /tmp/verify_rpc.py

# L3
python3 scripts/scan/provider_ua_parity_check.py .
python3 scripts/scan/provider_models_cache_wiring_check.py .

# L4
sh /var/minis/shared/tools/gate_run.sh --full --repo "$PWD" --chunks 8
```
