# FIX-META-provider-write-rules — 门见证收口（axis = anchor）

分支 `fix/meta-audit-m4-anchor-body` · 基线 `20a9fbadac1b0a78e3013e299c1694eaa2812e2e` · 支 M4
本轮只改**门的判据**（+ 见证固化成 selftest 臂），**生产代码一行未动**。

## 1. 门

`scripts/scan/provider_write_rules_check.py`（ARCH-01 / deep-audit-1039 A-04 的
"customBaseURL 归一化单一源" 门）。原 9 条判据 W1–W9，其中 W8/W9 由 1039 A-04 引入：
W8 = 声明 `normalizeCustomBaseURL` 存在且每个写入面**调用**它；W9 = 读侧
`effectiveBaseURL` 自己 trim。

## 2. 轴

`anchor` —— 判据钉的是**声明头 + 调用点**（`"internal fun normalizeCustomBaseURL" in pwr`
与 `NORMALIZE_CALL in face_body`），helper 自己的函数体内容是任意值。也就是说门能证明
"大家都走同一个函数"，但不能证明"这个函数还在做它该做的事"。

## 3. 见证（外部审计给出，本轮机械复证）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/config/collections/ProviderWriteRules.kt",
 "find":"internal fun normalizeCustomBaseURL(raw: String?): String? =\n    raw?.trim()?.takeIf { it.isNotEmpty() }",
 "replace":"internal fun normalizeCustomBaseURL(raw: String?): String? =\n    raw?.takeIf { it.isNotEmpty() }",
 "why_gate_green":"W8 只查声明头 `internal fun normalizeCustomBaseURL` 在文件里唯一存在、各写入面调用 normalizeCustomBaseURL(、且这些写入面没有内联 .ifEmpty{null}/takeIf{isNotBlank}。从不检查 normalizeCustomBaseURL 自己的函数体是否还有 .trim()。",
 "why_defect_back":"去掉 .trim() 后退化为 .ifEmpty{null}：全空格字符串 \"   \" 不再归一化为 null（clear），带首尾空格的 URL 跨所有写入面（minis-config / Debug RPC）被原样持久化（1039 A-04 重现）。"}
```

**修前**：见证补丁 → 门 `rc=0`（W1–W9 全绿）。
**修后**：同一补丁 → 门 `rc=1`，且红的恰好是 `w10` 一条：
`normalizeCustomBaseURL body lost its whitespace normalization (.trim()): '=\n    raw?.takeIf { it.isNotEmpty() }'`
（`w1..w9` 与 `fail_closed` 均为空 —— 边界干净，没有连带误伤。）

## 4. 现状判据 → 新判据

### 4.1 新增 W10：归一化**体**必须仍在归一化

```python
norm_body = policy_body(pwr, "normalizeCustomBaseURL")
if norm_body is None:
    detail["fail_closed"].append(...)          # 抽不出体 = fail-closed 红
else:
    if not TRIM_RE.search(norm_body):          # .trim() / .trimStart() / .trimEnd()
        detail["w10"].append("… body lost its whitespace normalization …")
    if not COLLAPSE_RE.search(norm_body):      # takeIf{ isNotEmpty/isNotBlank } | ifEmpty{ null }
        detail["w10"].append("… body lost its blank->null collapse …")
```

`TRIM_RE = \.trim[A-Za-z]*\s*\(`；`COLLAPSE_RE = takeIf\s*\{[^}\n]*isNot(?:Empty|Blank)\(\)|ifEmpty\s*\{[^}\n]*null`。
两半**都要在**：见证只砍了一半（trim）就足以让全空格串重新被持久化。

### 4.2 为什么不能用「该门已有的 `extract_function`」（任务书原建议）

实测：`extract_function(code, "normalizeCustomBaseURL")` 在真树上返回 **`'=\n    raw?.trim()'`**
—— 它遇到第一个**配平括号对**就收工（`trim()` 的 `)`），把 `?.takeIf { it.isNotEmpty() }`
整段丢掉。若直接拿它做判据：

- 真树（正确）→ body 里没有 collapse → **W10 误红**，门在干净树上就打自己脸；
- 见证补丁（错误）→ body 里没有 trim → 红，方向对了但判据本身是坏的。

因此新增**并列**抽取器 `expression_body()`：表达式体扫描在 depth 归零后，若下一个非空字符是
`.` / `?`（链式调用、Elvis）则继续，直到换行才收；括号不配平（body 未终止）返回 `None` =
fail-closed。`extract_function` **一字未改**（W5/W6/W7/W8 的块体判据依赖它，半径不动）。

`policy_body()` = 先 `expression_body`（表达式体）后 `extract_function`（块体，且要求以 `{` 开头）；
两条路都失败才返回 `None`。这样「把 helper 改写成块体但语义正确」仍然绿 —— 实测：

```
# 把 helper 改写成 { val t = raw?.trim() ?: return null; return t.takeIf { it.isNotEmpty() } }
check_tree(...) → rc=0，detail 全空
```

即 W10 钉的是**语义**（trim + 收窄），不是"必须是表达式体"这一拼写。

### 4.3 见证固化（selftest 新增 4 臂 + 1 臂期望修正）

| 臂 | 变异 | 期望红集 |
|---|---|---|
| S16 | **见证原文**：body 去掉 `.trim()` | `{w10}` |
| S17 | body 去掉空收窄（只剩 `raw?.trim()`） | `{w10}` |
| S18 | body 直接 `= raw`（helper 掏空成恒等函数） | `{w10}` |
| S19 | 真树原样（对照绿臂，专为 W10 复跑一次） | `{}` |
| S7（修正） | helper 改名（声明消失） | `{w8, fail_closed}` ← 原本只期望 `{w8}`；W10 抽不出体是 fail-closed，两处同时成立才是真实情况 |

S16 是**见证逐字入仓**：find 串与外部审计 JSON 的 `find` 完全一致，任何对
`ProviderWriteRules.kt` 函数体的改写都会让该臂 anchor-miss 直接炸（`replace_in` 的
`assert old in text`），而不是静默变成一个"什么也没改"的绿臂。

## 5. 四层证据

**L1（本门）**
- 真树：`python3 scripts/scan/provider_write_rules_check.py .` → `rc=0`（W10 遍历真树 body，两半都在）。
- `--selftest` → **20/20 PASS**，收尾标记 `PROVIDER_WRITE_RULES_SELFTEST=PASS`。
  新增臂输出原文：
  ```
  PASS S16-witness-body-drops-trim-W10 (rc=1 got=('w10',) exp=('w10',))
  PASS S17-body-drops-collapse-W10 (rc=1 got=('w10',) exp=('w10',))
  PASS S18-body-emptied-W10 (rc=1 got=('w10',) exp=('w10',))
  PASS S19-fixed-shape-green-W10 (rc=0 got=() exp=())
  PASS S7-normalize-helper-missing-W8 (rc=1 got=('fail_closed', 'w8') exp=('fail_closed', 'w8'))
  ```
  期望集全部**手写自判据表**，不从运行结果反推（否则变异会伪装成 PASS）。

**L2（机械复证，必过）**
```
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py <report-A> \
        --repo /var/minis/shared/work/meta-audit/wt-m4
── provider_write_rules_check.py
   [1] -  rc=1  restore=True  → 门抓住了（见证无效）
```
同一轮里其余 5 门仍 `GATE_BLIND_CONFIRMED`（不在本支文件集，未动）。
`restore=True`：补丁后用 sha256 复原，工作树零残留。

**L3（全量，收尾各跑一次）**
- `sh scripts/scan/scan.sh` → `rc=0`，尾部 `║ Summary: 112 passed, 0 failed`（门总数不变，未新增门）。
- `python3 scripts/scan/test_scan.py` → `rc=0`，`✅ ALL 344 CASES PASS`（夹具总数不变）。

**L4（CI）** 见 §6。

## 6. CI

`scripts/**` 不在 `build-apk.yml` 的 `paths` 里，push 不触发，必须 dispatch。
`head_sha` 三方核对（本地 / `branches` API / `run.head_sha`）逐字符一致。

- run：**40100764322**（job `120892255460`，`event=workflow_dispatch`）→ `conclusion=success`
- `head_sha = 95a46f5b6656d18843742aaca63bec108d656aa4`，与本地 HEAD **逐字符一致**
- 判据（判读 1,483,573 B 日志）：
  - `Summary: 112 passed, 0 failed` 恰 **1** 条（门总数不变）
  - 门头 `[N/112]` 共 112 条、`1..112` 连续无缺无重
  - `ALL 344 CASES PASS` 恰 1 条（夹具总数不变）
  - `PROVIDER_WRITE_RULES_SELFTEST=PASS` 在册（W10 的 4 条新臂在 CI 里真跑）
  - 26 步中唯一非 success = `Publish to Releases` **skipped**（main 门控，预期）
  - 游离 `FAILED` 行 **0**（不含 PASSED 子串者），编译错 `e: ` **0**
- 报告提交 `c1e0c423` 在 CI 之后：`git diff --name-only 95a46f5b HEAD -- . ':(exclude)fix-reports'` → **0 行**
  ⇒ 源码树与 CI 验过的树逐字节一致，绿结论对当前树成立。

## 7. 残留登记（不在本支动手）

1. **同族：`normalizeCustomModelId` 的身体同样无判据。** W1 只查声明存在、W6 只查
   `modelsAdd` 调用了它；把它的 `.trim()` 砍掉，本门仍然全绿 —— 与本次见证**同构**，
   只是载体不同（modelId 而非 customBaseURL）。触发条件：下一次审计给出该载体的见证，
   或有人改 `ProviderWriteRules.normalizeCustomModelId` 的函数体（改则必审）。
2. **同族：W1 的 `SHARED_RULE_DECLS` 全部是声明头。** `isCustomBaseURLWriteAllowed` /
   `canonicalModelProviderName` 的体被改成恒 `true` / 恒 `""`，本门不会红（W7 只查调用点，
   W6 只查 `modelsAdd` 里出现 `canonicalModelProviderName(`）。触发条件同上。
3. **`effectiveBaseURL` 已由 W9 覆盖身体**（`.trim()` + `trimEnd('/')` 两半都在体内），
   本次未扩半径。
4. W10 只断言"两半都在体内"，不断言**顺序**（`takeIf` 作用在 trim 之后）。理论上
   `raw?.takeIf { it.isNotEmpty() }?.trim()` 会通过 W10 —— 但它同样会让全空格串逃逸，
   属于残留序问题；未收紧的原因：现有真树与全部 fixture 都是正确顺序，加顺序断言属于
   "为未触发的形态加装置"。触发条件：出现该形态的提交。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m4
python3 scripts/scan/provider_write_rules_check.py .            # 真树 → rc=0
python3 scripts/scan/provider_write_rules_check.py --selftest    # 20 臂 + SELFTEST=PASS

# 见证（手工复证）
python3 - <<'PY'
p='src/android/app/src/main/java/com/rikkaminis/app/config/collections/ProviderWriteRules.kt'
s=open(p,encoding='utf-8').read()
old="internal fun normalizeCustomBaseURL(raw: String?): String? =\n    raw?.trim()?.takeIf { it.isNotEmpty() }"
assert s.count(old)==1
open(p,'w',encoding='utf-8').write(s.replace(old,"internal fun normalizeCustomBaseURL(raw: String?): String? =\n    raw?.takeIf { it.isNotEmpty() }"))
PY
python3 scripts/scan/provider_write_rules_check.py .            # → rc=1，只有 w10 红
git checkout -- src/android/app/src/main/java/com/rikkaminis/app/config/collections/ProviderWriteRules.kt
```

或直接走 `verify_witness.py <report-A> --repo .`（它会自己加补丁、跑门、复原并核对 sha256）。
