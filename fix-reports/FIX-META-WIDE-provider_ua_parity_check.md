# FIX-META-WIDE-provider_ua_parity_check

- 分支：`fix/meta-wide-02-provider-symmetry`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 波次：meta-audit-witness / wide-1024（组 B 报告）
- 生产代码改动：**0 行**（只改门判据 + 门自证臂）

## 1. 门

`scripts/scan/provider_ua_parity_check.py` — 四条判据族（工厂臂 UA 转发 / Gemini EOF 判据 / 语音族 UA / OpenAI 出站单一 api-key + UA 汇聚点），本次加固 3 处：
**ISSUE-04c（check_gemini）**、**ISSUE-04a（check_factory）**、**V3（check_voice）**。

## 2. 轴

- ISSUE-04c、ISSUE-04a：**dataflow** —— 参数名/累加语句「存在」≠ 值到达使用点（helper 边界切开一次，右值写成 `null` 一次）。
- V3：**order** —— 存在性正则对最终值（post-condition）无感知：注入存在，但被同块随后的赋值覆盖。

## 3. 见证（3 条，均已机械复证）

| # | 判据 | 见证 | 修复前 | 修复后 | 恢复 |
|---|---|---|---|---|---|
| 1 | ISSUE-04c | `geminiEofTerminal(lastFinishReason, contentChars, toolChars)` → `… , 0)` | rc=0（门瞎） | rc=1 | sha256 一致 |
| 2 | ISSUE-04a | openRouter 臂 `customUserAgent = instance.customUserAgent,` → `customUserAgent = null,` | rc=0（门瞎） | rc=1 | sha256 一致 |
| 3 | V3 | `.also { it?.customUserAgent = instance.customUserAgent }` → 末尾追加 `; it?.customUserAgent = null` | rc=0（门瞎） | rc=1 | sha256 一致 |

反攻脚本：`/tmp/verify_ua.py`（原文计数必须恰 1 → 打补丁 → 旧门（`git show HEAD:` 版）与当前门各跑一次 → `git checkout --` 恢复 → 比 sha256）。输出：

```
gemini-terminal-zero old_rc=0 new_rc=1 restore=True
factory-ua-null      old_rc=0 new_rc=1 restore=True
voice-clobber        old_rc=0 new_rc=1 restore=True
```

**守卫不动**：任务书点名「不要动也不要删」的 D2b 形状（`buildModelsRequest(url, apiKey, azureMode, customUserAgent)` → 第三参传 `false`）复测 —— `rc=1`，restore=True。

## 4. 修法

- **ISSUE-04c**：`TOOL_ACCUM_RE` 从累加语句 `X += toolCallActivityChars(` **派生**计数器标识符（不再按名字认 `toolChars`），再遍历所有 `geminiEofTerminal(` **调用点**（用 `(?<!fun )` 形状排除声明本身），要求实参里出现该累加器；括号失衡 ⇒ fail-closed 红（消息含 "fail-closed"）。派生不到累加器时沿用原有「累加语句缺失」红。
- **ISSUE-04a**：`UA_ARG` 从「参数名 + 等号」升级为**值数据流** `UA_FLOW_RE`（取 `customUserAgent =` 的右值），`ua_value_flows` 判定：右值含 `.customUserAgent`（任意接收者名，不锚 `instance`），或经一个本地别名/一跳 helper（`decl_text` 同时支持 `{}` 体与 `= expr` 体）触达字段。字面量（`null`/`""`）一律红。
- **V3**：保留原有「注入必须存在」，新增**后置条件**——`make()` 体内**每一条**对 `*customUserAgent` 的赋值右值都必须携带实例覆盖值；任何一条是字面量即「assign-then-clobber」红（消息点名被覆盖的目标与右值）。

**自证臂**（SELFTEST 收尾标记一字未改）：

| 臂 | 断言 | 形态 |
|---|---|---|
| GOOD_GEMINI_TERMINAL（clean 臂） | 0 violation | 接受臂：真 provider 的 helper 形状（`val terminal = geminiEofTerminal(…, toolChars)`） |
| BAD_GEMINI_TERMINAL_ZERO | 红含「does not pass the tool counter」 | 见证 1 |
| BAD_GEMINI_TERMINAL_UNBALANCED | 红含「unbalanced (fail-closed」 | parser-death fail-closed 臂 |
| BAD_FACTORY_UA_NULL | 红含「the instance override never arrives」 | 见证 2 |
| GOOD_FACTORY_UA_ALIAS（clean 臂） | 0 violation | 接受臂：`val ua = instance.customUserAgent` → `customUserAgent = ua` |
| BAD_VOICE_FACTORY_CLOBBER | 红含「assign-then-clobber」 | 见证 3 |

selftest 逐行输出新增四行计数（`voice factory clobber` / `gemini terminal-zero` / `factory UA null` / 各 fixture 名），便于 CI 日志里直接数臂。

## 5. 四层证据

- **L1 本门（真树）**：`python3 scripts/scan/provider_ua_parity_check.py .` → `PASS provider_ua_parity_check (...)` rc=0；`--selftest` → 全臂符合预期 + `PROVIDER_UA_PARITY_SELFTEST=PASS`。
- **L2 见证反攻**：上表 3/3 `old_rc=0 → new_rc=1`，restore=True；D2b 守卫形状仍 rc=1。
- **L3 邻门**：`provider_rpc_symmetry_check` rc=0、`provider_boundary_guard` rc=0。
- **L4 全量**：`scan.sh` → `Summary: 112 passed, 0 failed`；分支 CI **ALL 344 CASES PASS**（见 §6）；本机 `test_da1019_gates` / `test_da1025_gates` / `test_da1026_gates` 单组复跑全 ✅。门数 / 夹具数不变：112 / 344。

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

1. **ISSUE-04c · 认名不认值**：调用点必须出现累加器标识符，但 `geminiEofTerminal(lastFinishReason, contentChars, contentChars)`（拌错参数）仍绿。
   - 触发条件：有人传错参数但不传字面量。
   - 触发后方案：把实参按位置与 helper 形参表对齐（解析 `geminiEofTerminal` 签名，第 3 个形参位置必须吃累加器）。
   - 搁置理由：需要形参位次对齐，属另一轴（`signature`），本波见证不涉及；真树与臂都只走「值被换成字面量」这一种形态。
2. **ISSUE-04a · 接收者不校验**：`customUserAgent = other.customUserAgent` 判绿（只要求「来自某个 `.customUserAgent`」）。
   - 触发条件：多人多实参时取错实例字段。
   - 触发后方案：接收者必须与被构造的实例同源（`instance` 或本函数入口的同一形参）。
   - 搁置理由：真树七个臂全写 `instance.customUserAgent`；加接收者白名单等于锚拼写，与「形态等价类」原则冲突，等真出现错接收者再定只写。
3. **V3 · 越函数边界的覆盖**：`make()` 体内若把对象交给 helper 覆盖（`finish(built)` 里置 null）不可见。
   - 触发条件：覆盖被搬进被调函数。
   - 触发后方案：跟随一跳 helper（与 04a 同一套 `decl_text`）。
   - 搁置理由：当前写法是内联 `.also{}`；跨函数覆盖需要 helper 副作用建模，装置比例不符。

## 8. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-02-provider-symmetry

# L1
python3 scripts/scan/provider_ua_parity_check.py .
python3 scripts/scan/provider_ua_parity_check.py --selftest

# L2（三条见证，含旧门对照与 sha256 恢复校验）
git show HEAD:scripts/scan/provider_ua_parity_check.py > /tmp/old_ua_gate.py
python3 /tmp/verify_ua.py

# L3
python3 scripts/scan/provider_rpc_symmetry_check.py .
python3 scripts/scan/provider_boundary_guard.py .

# L4
sh /var/minis/shared/tools/gate_run.sh --full --repo "$PWD" --chunks 8
```
