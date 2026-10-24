# FIX-META-auto-approve-bypass — 门的「效果未消费」轴加固

- 波次：meta-audit-1024 / 支 M1（axis = dataflow）
- 分支：`fix/meta-audit-m1-guard-consume`
- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- 提交：`16e5a666a9f60432a4d5bb13d434aa429a6f8c44`（判据加固）+
  `cfef92240c4d1c66bb193d4c6b3c4aa859bc94f4`（C5 换行边界修复 + 回归臂）→ HEAD = `cfef9224`
- 门：`scripts/scan/auto_approve_bypass_check.py`
- 生产代码：**零改动**（只修判据 + 把见证固化成 selftest 臂）

## 1. 门

`auto_approve_bypass_check` 钉住「免确认模式」的三个单点决策：
C1 `ConfigBridge.performWriteBatch` 必须咨询 `ConfigAutoApprovePolicy.shouldSkip`；
C2 `ConfigConfirmationGate.requestConfirmation` 生产代码里恰好一个调用点；
C3 `OffloadPermissionManager.getLevel` 必须经 `OffloadPermissionLevelPolicy.resolve`
（纯优先级表）解析，不许内联 ad-hoc 叠层；C4 免确认模式不得出现在 `ConfigRegistry` /
`ConfigBuiltins`（agent 不能自我授权）。

## 2. 轴

**dataflow —— 调用了 ≠ 用上了。**

C3 的判据是 `"OffloadPermissionLevelPolicy.resolve" in level_body`：只要**符号出现**在
`getLevel` 函数体里就算通过。这问的是「有没有调用表」，不是「表的结果有没有成为返回值」。
于是最小补丁可以把调用保留成一条丢弃语句，再无条件返回一个等级——符号检查照绿，
而用户配置的每一行权限（NOT_ALLOWED / ASK_ONCE）全部失效。

## 3. 见证（外部对抗审计，已机械复证）

`src/android/app/src/main/java/com/rikkaminis/app/offload/OffloadPermissionManager.kt`

```diff
-        return OffloadPermissionLevelPolicy.resolve(
+        OffloadPermissionLevelPolicy.resolve(
             storedLevel = stored,
             defaultLevel = info.defaultLevel,
             isPrivilegedTool = info.category == PermissionCategory.INTEGRATIONS,
             autoApproveEnabled = AutoApproveStore.isEnabled,
             privilegedToolsEnabled = AutoApproveStore.isPrivilegedToolsEnabled,
         )
+        return PermissionLevel.BYPASS
```

- 打补丁后门 **rc=0**（`GATE_BLIND_CONFIRMED`，restore=True）。
- 缺陷后果：`resolve(...)` 成了纯求值后丢弃的表达式；`getLevel` 对**所有** offload 工具
  返回 `BYPASS`（免确认），用户显式设置的 `NOT_ALLOWED` / `ASK_ONCE` 被架空，
  免确认模式的「单点决策」设计整体失效——这是权限面的 P0 级后果。

## 4. 现状判据 → 新判据

### 新增 C5（唯一被证伪的轴）

```
C5: every OffloadPermissionLevelPolicy.resolve() in getLevel must BE the returned value
```

实现（`check_policy_return`）：

1. 在 `getLevel` 函数体内枚举所有 `OffloadPermissionLevelPolicy.resolve(` 调用点。
   （**零个调用点不产出 C5**——«符号在不在» 由 C3 负责，避免同一条缺陷双重计数。）
2. 对每个调用点，用**字符串感知**的反向扫描定位它所在的**语句起点**：
   `{` / `}` / `;` 以及**嵌套深度 0 上的换行**（Kotlin 语句由换行分隔）都是语句边界；
   括号内的换行不是边界（多行参数表正确归属）；扫描走出外层表达式即视为不可归属。
3. 语句头是 `return` / `return@label` / `throw` ⇒ **消费合格**（`return resolve(...)`）。
4. 语句头是 `val x = …` / `var x: T = …` ⇒ 再看函数体里后续是否存在
   `return x`（含 `return@label x`）；存在 ⇒ 合格（「赋值给被 return 的变量」）。
5. 其余（裸丢弃调用、`_ = resolve(...)`、丢弃后跟任意无条件 return）⇒ **违规**。
6. **fail-closed**：语句归属解析不出来时，语句头必然不像消费者 ⇒ 违规；不静默通过。

判据锚在「**这个值有没有成为 getLevel 的返回**」这个决定点上，不锚拼写：直接 return、
先绑定后 return、`return@` 标签返回都被接受（各有臂覆盖）。

C1–C4 一字未改。

## 5. 四层验证

**L1 本门真树 + selftest**

- `python3 scripts/scan/auto_approve_bypass_check.py .`
  → `AUTO_APPROVE_BYPASS_CHECK: OK (all 5 criteria hold)`，rc=0。
- `--selftest` → `AUTO_APPROVE_BYPASS_SELFTEST: PASS`，**7 臂**（原 5 + 新 2）：

| 臂 | 期望 | 说明 |
|---|---|---|
| `compliant` | 0 违规 | 真树 |
| `policy_consult_removed` | 1 | 旧臂（C1） |
| `second_call_site` | 1 | 旧臂（C2） |
| `getlevel_inlined` | 1 | 旧臂（C3）——**计数未变**，新判据不误伤 |
| `registry_exposure` | 1 | 旧臂（C4） |
| `getlevel_result_discarded` | **1** | 本次外部见证的**逐字复刻**（C5 独一无二地红） |
| `getlevel_via_local_val` | **0** | 精度臂：`val x = resolve(...); return x` 必须绿 |
| `getlevel_discard_return_other_val` | **1** | 自查发现的假绿回归臂：`丢弃调用 + return stored ?: …` |

关键点：`getlevel_result_discarded` 恰好 1 条违规 ⇒ **只有 C5 红、C3 仍绿**，
这正是「旧门看不见它」的机械证明；`getlevel_via_local_val` 绿 ⇒ 判据锚的是消费关系
而不是语法形态。

**★ 自查抓到过一次自己的假绿（已闭环，值得记）**：首版 `statement_start` 只把
`{`/`}`/`;` 当语句边界，而仓内 Kotlin 几乎不用分号 ⇒ 反向扫描**跨行**把上一条
`val stored = prefs.getString(…)` 卷进同一「语句头」，`RE_LOCAL_BIND` 于是匹配到
`stored`，再搜到后面的 `return stored ?: info.defaultLevel`，把「丢弃调用 + 返回别的
局部值」判成 GREEN。**判据锚的其实不是被调用值的去向，而是「语句头里有个 val」** ——
正是本门要防的那类空转。修法：深度 0 的换行也是语句边界（括号内的换行不是），并把该
形态固化成 `getlevel_discard_return_other_val` 臂（期望 1 条违规）。

**L2 机械复证（必过）**

```
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md \
  --repo /var/minis/shared/work/meta-audit/wt-m1
```

→ `auto_approve_bypass_check.py`：`gate rc=1 → ❌ 门抓住了（见证无效）  restore=True`，
输出尾部为 `C5: getLevel discards the OffloadPermissionLevelPolicy.resolve() result
(call at offset 297, statement head 'OffloadPermissionLevelPolicy.resolve(')`，即
**GATE_CAUGHT_WITNESS**。

**L3 全量回归（冻结面未动）**

- `bash scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`，rc=0。
- `python3 scripts/scan/test_scan.py` → `✅ ALL 344 CASES PASS`，rc=0。
- 冻结面零触碰：`test_scan.py` 里本门的 selftest 标记串
  `AUTO_APPROVE_BYPASS_SELFTEST: PASS` 保持逐字一致。

**L2.5 独立变体探针（修前修后都跑）**

| 变体 | 实测 | 判定 |
|---|---|---|
| `val lvl = resolve(…); return lvl` | GREEN | 接受的第二种拼写 |
| `resolve(…)` 丢弃 + `return stored ?: default` | RED | 修前**假绿**，已修（R6 之外的新发现） |
| `return run { resolve(…) }` | RED | fail-closed（见 R7） |

**L4 CI** — 见 §6。

## 6. CI

- dispatch：`gh-actions-dispatch --workflow build-apk.yml --ref fix/meta-audit-m1-guard-consume`
  （`scripts/**` 不在 build-apk.yml 的 paths 里，分支 push 不触发）。
- **run #2625 / id `40103251158` / job `120899545237`**：attempt 1，
  `event=workflow_dispatch`，**conclusion=success**。
- **三方 sha 逐字符一致**：本地 HEAD = 远端分支头 = `run.head_sha` =
  `cfef92240c4d1c66bb193d4c6b3c4aa859bc94f4`。
- 步骤判读：**26 步全 success，唯一 `skipped` = step 23 `Publish to Releases`**
  （`if: github.ref == 'refs/heads/main'` 门控，分支 run 预期行为）。
- 日志判读（job 日志 1,479,691 B / 17,842 行）：
  - 门汇总行恰一条：`║  Summary: 112 passed, 0 failed`；
  - `✅ ALL 344 CASES PASS` 恰一条；
  - 门头 112 条、编号 `[1/112]..[112/112]` **互异**、分母全为 112；
  - 两门 selftest 标记各出现 1 次（`AUTO_APPROVE_BYPASS_SELFTEST: PASS` /
    `OPEN_URL_BROKER_SCHEME_SELFTEST=PASS`）＝ test_scan 断言通过；
  - 编译错误 ` e: ` **0** 条；
  - 含 `FAILED` 的 6 行逐条确认**全是 PASSED 用例名子串**
    （`slot denied produces FAILED` 等），无游离失败；
  - 注：门 `--selftest` 的**逐臂 stdout 不进 CI 日志**（与 1042-B5 的结论一致），
    臂的存在性由「本地 `--selftest` 逐臂打印 + CI 里标记串断言」两源覆盖。
- 另记：run #2620（`16e5a666`，即 C5 换行边界修复**之前**的树）同样 success，
  仅作对照；最终结论一律以 #2625 为准。

## 7. 残留登记（只登记，不动手）

- **R1 值被 return 了，但「值本身是对的」不在本门视野**：C5 只保证
  `resolve(...)` 的结果是返回值，不保证 `resolve` 内部的分级表正确（那是
  `OffloadPermissionLevelPolicy` 的 JVM 单测面）。触发条件：优先级表改动。
- **R2 `if (…) return resolve(x) else return BYPASS` 形态**：语句头是 `return` ⇒ 绿。
  语义上合法（真分支返回策略值），但本门无法判断 `else` 臂的宽度是否越权。
  这是分级表 + 单测的职责。触发条件：出现 else 臂返回常量等级的写法。
- **R3 只覆盖 `getLevel`**。若未来新增第二个返回权限等级的函数（比如
  `getLevelForSession`），C5 不会自动扩过去。触发条件：出现第二个权限等级的解析入口。
  届时应把判据从「函数体枚举」升级为「所有 `PermissionLevel` 返回点的调用图闭包」
  （与 FIX-F9 的做法同族）。
- **R4 语句归属的反向扫描在「调用嵌套在别处参数里」时按 fail-closed 判红**
  （`foo(Policy.resolve(...))` 这种形态会被判违规）。方向安全，且真树无此形态。
  触发条件：真树出现把策略值当参数传递的调用点。
- **R5 零调用点时不产出 C5**（C3 负责）。若有人把 C3 也删了，则「压根没策略表」
  会变成 C3 单独红——门仍然红，无静默洞。
- **R6 与 `getlevel_inlined` 旧臂的边界**：该臂把符号改名 ⇒ C3 红、C5 因零调用点不产出 ⇒
  仍然恰好 1 条违规，旧臂计数未变（已实测）。
- **R7 值被包裹在表达式里时按 fail-closed 判红**：`return run { Policy.resolve(…) }`、
  `return with(foo) { Policy.resolve(…) }`、以及「上一行以运算符续行的调用」都判 RED
  （实测）。方向安全（不静默放行），但这些合法形态需要同步更新判据。触发条件：真树
  出现把策略调用包在 `run`/`with`/`let` 里的返回点；届时把判据从「语句头」升级为
  「返回表达式的值来源闭包」。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m1

# 本门（真树 + 带牙自测）
python3 scripts/scan/auto_approve_bypass_check.py .
python3 scripts/scan/auto_approve_bypass_check.py --selftest

# L2 机械复证（应打印「门抓住了（见证无效） restore=True」）
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md --repo "$PWD"

# L3 全量
bash scripts/scan/scan.sh          # Summary: 112 passed, 0 failed
python3 scripts/scan/test_scan.py  # ALL 344 CASES PASS

# 手工重现见证（可选）
# 把 OffloadPermissionManager.getLevel 的 `return OffloadPermissionLevelPolicy.resolve(`
# 的 `return ` 去掉，并在调用后加 `return PermissionLevel.BYPASS`：
# 改前 rc=0，改后 rc=1 且唯一违规是 C5。
```
