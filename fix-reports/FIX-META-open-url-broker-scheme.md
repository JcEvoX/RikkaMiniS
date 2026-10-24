# FIX-META-open-url-broker-scheme — 门的「效果未消费」轴加固

- 波次：meta-audit-1024 / 支 M1（axis = dataflow）
- 分支：`fix/meta-audit-m1-guard-consume`
- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- 提交：`16e5a666a9f60432a4d5bb13d434aa429a6f8c44`（判据加固）+
  `cfef92240c4d1c66bb193d4c6b3c4aa859bc94f4`（C5 换行边界修复，见姊妹报告；本门不受影响，
  只为让两条门共享同一 CI 树）→ HEAD = `cfef9224`
- 门：`scripts/scan/open_url_broker_scheme_check.py`
- 生产代码：**零改动**（只修判据 + 把见证固化成 selftest 臂）

## 1. 门

`open_url_broker_scheme_check` 钉住 `terminal/MinisOpenUrlBroker.kt` 的 scheme 白名单契约：
两个 `offer` 重载都必须经过唯一的 `isSupportedScheme` 门，任何 scheme 在落到
`_pendingUrl` 之前被拒；顺带钉住消费者侧（`TerminalScreen` 只消费 web scheme）与两条
terminal deep-link 入口的 sessionId 解析。

原本 14 条判据：U1（`offer(Uri)` 体内出现 `isSupportedScheme(` 子串）、U2a（String 重载
要么自门要么委托）、U2b（全文件恰好一个非 null `_pendingUrl.value =` 写点）、U3（门的下标
早于发布点下标）、U4（白名单本身不弱化）、U5a/b/c、U6a/b/c、U7a/b/c。

## 2. 轴

**dataflow —— 守卫的存在 ≠ 守卫生效。**

U1 只问「符号在不在函数体里」，U3 只问「出现的字符下标是否在发布点之前」。两者都是
**文本相邻性**判据，没有一条问「这个守卫分支真的会终结控制流吗」。于是最小补丁
`return` → `Unit` 把守卫改成空操作，两条判据同时满足，门继续绿。

## 3. 见证（外部对抗审计，已机械复证）

`src/android/app/src/main/java/com/rikkaminis/app/terminal/MinisOpenUrlBroker.kt`

```diff
-        if (!isSupportedScheme(uri.scheme)) return
+        if (!isSupportedScheme(uri.scheme)) Unit
         _pendingUrl.value = uri
```

- 打补丁后门 **rc=0**（`GATE_BLIND_CONFIRMED`，restore=True）。
- 缺陷后果：`if` 条件仍然求值，但 then 分支是个 `Unit` 表达式 = 空操作；`offer(uri)`
  把 `file://`、`javascript:`、`content://` 无条件写进 `_pendingUrl`，broker 的
  白名单契约整体失效（下游 `UrlPreviewSheet` / `DeepLinkHandler` 会拿到未消毒的 URI）。

## 4. 现状判据 → 新判据

### 新增 U8（唯一被证伪的轴）

```
U8 isSupportedScheme guard terminates control flow (no Unit/no-op arm)
```

判据实现（`analyse_guard_termination`）——在 `offer(uri: Uri)` 体内：

1. 枚举所有 `if (...)`，取条件里含 `isSupportedScheme(` 的那些（**守卫必须是某个 `if`
   的条件**；裸调用语句直接违规）。
2. 判定条件是否被否定：`!isSupportedScheme(…)`、`!(…)`、`….not()`、`… == false`／`.. != true`。
3. **否定型（早退）**：其 consequence（花括号块或单语句）内，**在块自身的深度 0 上**
   必须出现 `return` / `return@label` / `throw`（字符串感知、深度感知——lambda 里的
   `return` 不算），且发布点不得位于这条死臂内部（否则是「反向守卫」）。
4. **肯定型（包裹）**：`_pendingUrl.value = uri` 必须位于该 `if` 的 consequence 内。
5. 任一守卫满足 (3) 或 (4) ⇒ 绿；**否则红**。
6. **fail-closed**：条件括号失配、consequence 无法解析、或没有任何守卫满足 ⇒ 违规，
   不静默通过。

判据锚在「**这个守卫分支是否终结控制流**」这个决定点上，不锚任何一种拼写：
`return` / `return null` / `return@offer` / `throw …` / 多语句块（先打日志再 return）
/ 正向包裹 / `.not()` / `== false` 全部接受（各自的 selftest 臂覆盖）。

`U1`/`U2b`/`U3`/`U4` 一字未改：新判据只补「效果」半边，不替换旧判据。

## 5. 四层验证

**L1 本门真树 + selftest**

- `python3 scripts/scan/open_url_broker_scheme_check.py .` → rc=0，**15 条判据全绿**
  （原 14 条 + U8）。
- `--selftest` → `OPEN_URL_BROKER_SCHEME_SELFTEST=PASS`。新增 7 条臂：

| 臂 | 期望 | 说明 |
|---|---|---|
| `BAD_GUARD_UNIT` | **U8 红，其余 5 条全绿** | 本次外部见证的**逐字复刻** |
| `BAD_GUARD_EMPTY_BLOCK` | U8 红，U3 绿 | 空块也不终结 |
| `BAD_GUARD_INVERTED` | U8 红 | 被拒分支里发布（反向守卫） |
| `GOOD_GUARD_BLOCK_MULTI` | 全绿 | 多语句块（日志 + return） |
| `GOOD_GUARD_THROW` | 全绿 | `throw` 早退 |
| `GOOD_GUARD_POSITIVE_WRAP` | 全绿 | 正向包裹发布点 |
| `GOOD_GUARD_NOT_CALL` | 全绿 | `….not()` 否定 |

其中第一条是关键：**只红 U8、旧判据全绿**，且判据是「旧门看不见它」的直接证明。

**L2 机械复证（必过）**

```
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md \
  --repo /var/minis/shared/work/meta-audit/wt-m1
```

→ `open_url_broker_scheme_check.py`：`gate rc=1 → ❌ 门抓住了（见证无效）  restore=True`，
即 **GATE_CAUGHT_WITNESS**（其余门的输出行不属本支，忽略）。

**L3 全量回归（冻结面未动）**

- `bash scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`，rc=0。
- `python3 scripts/scan/test_scan.py` → `✅ ALL 344 CASES PASS`，rc=0。
- `scripts/scan/scan.sh`、`test_scan.py` **零触碰**（两门的注册行与 selftest 标记串未变：
  `OPEN_URL_BROKER_SCHEME_SELFTEST=PASS` 保持逐字一致）。

**L2.5 独立变体探针（修前修后都跑，用来查假绿/假红）**

| 变体 | 实测 | 判定 |
|---|---|---|
| `val ok = isSupportedScheme(…); if (!ok) return` | RED | fail-closed（见 R7） |
| `if (!(isSupportedScheme(…))) return` | GREEN | 双层括号否定仍被识别 |
| `if (gate) { … } else { _pendingUrl.value = uri }` | RED | fail-closed（见 R5） |
| `if (!gate(…))` 换行 `return` | GREEN | consequence 提取跨换行有效（见 R4） |

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

- **R1 门的判据只覆盖 `offer(uri: Uri)` 体内**。若未来有人在别的函数里新增
  `_pendingUrl.value = …` 写点，U2b（恰好一个写点）会红，但 U8 不会指出它没被守卫
  保护——U2b 只是数量约束。触发条件：出现第二个写点。
- **R2 `isSupportedScheme` 之外的第三方守卫**（例如调用方自行 `require(scheme in …)`）
  不在 U8 视野内；本门锚的是 broker 自己的唯一门。
- **R3 `isWebScheme`（消费者侧白名单）没有等价的「终结/包裹」判据**。U5c 已钉住
  `consume()` 必须在 `if (isWebScheme(...))` 分支内，形态上已要求包裹；但若把分支体
  改成 `Unit` 之外的非 consume 语句，U5c 会红（没有 consume 命中），无静默洞。
  触发条件：消费者侧出现第二种消费点。
- **R4 单语句 consequence 的跨界行为（实测）**：`if (!gate(x)) return` 这类同行形态绿；
  把 `return` 写在**下一行**（`if (!gate(x))\n    return`）也绿——consequence 提取会跳过
  空白（含换行）后再取语句，实测 `gate-then-no-return-multiline` 变体为 GREEN。行内注释
  已在 `strip_comments` 阶段抹掉，不参与判定。
- **R5 `else` 分支未建模**：`if (gate(x)) { … } else { _pendingUrl.value = uri }` 这种把发布
  放进 else 的写法**红**（实测 `else-branch publish` → RED）。U8 只看 then 与 publish 的
  位置关系，方向是 fail-closed，安全。
- **R7 间接守卫（先算成局部值再判）实测为 RED**：`val ok = isSupportedScheme(uri.scheme)`
  + `if (!ok) return` 这种把调用移出 `if` 条件的写法会被判「守卫不是 if 的条件」——
  方向 fail-closed，不静默放行，但这类合法重构需要同步更新判据（或用正向包裹/内联写法）。
  触发条件：真树出现把守卫值先绑定再判的形态；届时按 FIX-F9 的做法把判据扩成
  「条件里可达的布尔来源闭包」。
- **R6 与 U1 的关系**：无守卫调用时 U8 **不产出判据**（返回空），避免与 U1 重复计数；
  这不让门变松——U1 在那种形态下本来就红。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m1

# 本门（真树 + 带牙自测）
python3 scripts/scan/open_url_broker_scheme_check.py .
python3 scripts/scan/open_url_broker_scheme_check.py --selftest

# L2 机械复证（应打印「门抓住了（见证无效） restore=True」）
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md --repo "$PWD"

# L3 全量
bash scripts/scan/scan.sh          # Summary: 112 passed, 0 failed
python3 scripts/scan/test_scan.py  # ALL 344 CASES PASS

# 手工重现见证（可选）
# 把 MinisOpenUrlBroker.kt 里 `if (!isSupportedScheme(uri.scheme)) return`
# 改成 `... Unit` 后重跑本门：改前 rc=0，改后 rc=1。
```
