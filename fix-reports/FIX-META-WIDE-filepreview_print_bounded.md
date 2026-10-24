# filepreview_print_bounded_check — 文件预览打印槽 / SAF 读取上限 / produceState 键（wide-1024）

> wide-1024 门见证加固 · 分支 `fix/meta-wide-08-file-preview` · 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
> 门文件 `scripts/scan/filepreview_print_bounded_check.py` · sha256 前 16 位 `bcd77384e0194cfc`
> 本波**零生产代码改动**：五个 `.kt` 生产文件首字节未变（见 §5 L2 的 restore 记录）。

## 1. 门

`filepreview_print_bounded_check.py` — 钉住 `ui/sandbox/FilePreviewScreen.kt` 的打印槽守卫、`ui/settings/SafImportRead.kt` 的 SAF 读取上限、以及 PDF 预览 `produceState` 的键值来源。

自证标记（冻结面 `test_scan.py` 全等断言，**一字未改**）：`FILEPREVIEW_PRINT_BOUNDED_SELFTEST=PASS`

## 2. 轴

三条都是同一族：**判据锚「某物存在」，不锚「该物的效果达成」**。

- B-05 → `order` + control-flow：身份比较存在 ≠ 被取代的视图被挡住。
- B-07 → `dataflow`：调用了 `readAtMost(` ≠ 上限是调用方的 cap。
- C-PDF-KEY → `spelling` → 值：键里写着 `item.file` ≠ 运行期键跟着文件走。

## 3. 见证（修复前 rc=0 / 修复后 rc≠0）

| 见证 | 旧判据为何看不见 | 新判据凭什么抓住 |
|---|---|---|
| B-05 `if (false && printWebView !== view)` | 只要求「首个 print 调用之前出现过 `printWebView !== view`」——字面量短路后该子串仍在，死分支照样通过 | 解析首个 print 之前**每一个** `if (…)`：条件含身份比较即视为守卫；条件含 `false &&` / `true \|\|` 判死；守卫分支体必须以 `return`/`throw` 结束（或接受的 helper 体内必须有 `===`）。缺守卫 / 死守卫 / 不终结 三条各自红 |
| B-07 `readAtMost(input, Long.MAX_VALUE - 1L)` | 只查 `readAtMost(` 字面出现——被换掉的只是第二个实参 | 在 `readSafBytes` 体内解析 `readAtMost(...)` 的实参表，第二个实参必须是 `cap` 或 `minOf(cap, …)`；实参表读不出来 → fail-closed 红 |
| C-PDF-KEY `key2 = item.file.let { 0 }` | 只查键表达式里「包含 `item.file` 子串」——`.let { 0 }` 让运行期键变成常量 0，切文件不复位 | 键必须是**纯引用链**（`KEY_REF_RE` 全匹配 `item.file` / `idx` / `renderer.epoch`），且整条表达式（不只看子串）等于/前缀于某个数据源；链上有 lambda / 字面量一律红 |

## 4. 修法

1. 新增 `_if_statements()`：解析 `if (…)` 的花括号体与单语句体（`${…}` 花括号成对，深度计数安全）。`check_print_slot` 改用它枚举首个 print 前的守卫，并加 `DEAD_GUARD_RE`（`false &&` / `true ||`）与 `CONTROL_FLOW_END_RE`（`return`/`throw`）两条判据；`isCurrentPrintView(...)` 这种 helper 写法允许，但 `_decl_text()` 读它的体，体里没有 `===` 判红（anchor 规则：按函数体认定）。
2. 新增 `_args_of()`：括号感知的顶层实参切分（读不出 → `None`），`check_saf_imports` 用它校验 `readAtMost` 的第二实参。
3. 新增 `KEY_REF_RE`，`check_preview_keys` 对每个 key 做「纯引用链」全匹配，并把「数据源判定」从子串包含改成 `k == src or k.startswith(src + ".")`。
4. **顺带修掉本门自证的一处静默失效**：`slot_arms` 失败分支原本写 `ok = False`（该函数里 `ok` 未定义），一旦臂失败只会抛 `NameError` 或根本不失败；现在失败臂进 `failures`，收尾 `return 1` 并打印 `..._SELFTEST=FAIL`。

设计约束（本波五门共用）：
- **解析失败 fail-closed**：括号/花括号失配一律报违规，消息里点明 `fail-closed`，绝不静默返回绿。
- **锚决定点，不锚拼写**：判据读的是「谁决定了这个值/这条控制流」，不是「文件里有没有这个词」。
- **不误伤合法写法**：真树必须仍绿；只把判据放宽到能过真树、同时仍抓住见证的那一档。

## 5. 四层证据

### L1 本门

    python3 scripts/scan/filepreview_print_bounded_check.py .        -> rc=0（真树绿）
    python3 scripts/scan/filepreview_print_bounded_check.py --selftest -> FILEPREVIEW_PRINT_BOUNDED_SELFTEST=PASS

新增/改动臂（fixture 断言「恰好红对应判据」）：

    b05-guard short-circuited        （新）`false && printWebView !== view` → 红 "short-circuited"
    b05-guard without return         （新）守卫体删掉 return     → 红 "does not end control flow"
    b05-helper guard (clean)         （新）`!isCurrentPrintView(...)` + 体内 `===` → 绿
    b05-helper guard compares equality（新）helper 体改 `==`  → 红 "does not compare identity"
    pdf-key-opaque                   （新）`key2 = item.file.let { 0 }` → 红 "not a plain data-source reference"
    helper_unbounded_cap             （新）`readAtMost(input, Long.MAX_VALUE - 1L)` → 红 "must be the caller's cap"
    原有 8 条槽臂 / SAF 臂 / preview-set 臂全部保持原判

### L2 见证反攻（唯一有效证据）

`probe/reattack_wide08.py`：对每条见证「打补丁 → 跑本门 → 必须 rc≠0 → 恢复 → sha256 比对」。

    B-05 identity guard short-circuited          rc=1 restore=True
B-07 readAtMost with an unbounded cap        rc=1 restore=True
C-PDF-KEY produceState key constant          rc=1 restore=True

判据链：同一补丁在修复前 rc=0（门瞎）→ 修复后 rc≠0（门抓住）⇒ **当初打穿它的那把刀现在被挡住**。
15 条见证 **15/15 CAUGHT**，15 次 restore 全部 `sha256` 一致（`git status --porcelain` 只剩五个门文件）。

### L3 邻门

    file_provider_paths_coverage_check      rc=0
    offload_cli_args_check                  rc=0
    offload_host_path_check                 rc=0
    offload_media_b64_wire_check            rc=0
    offload_relative_path_unify_check       rc=0
    skill_file_save_result_check            rc=0

### L4 全量

    bash scripts/scan/scan.sh      -> Summary: 112 passed, 0 failed
    python3 scripts/scan/test_scan.py -> ALL 344 CASES PASS

## 6. CI

分支 `fix/meta-wide-08-file-preview`，commit 见本文件所在提交；push 后手动 dispatch（`on: push` 只绑 main）。

- run id：`<见最终回报 / CI>`，`conclusion=<success>`，`head_sha` 与本支 HEAD 逐字符一致。
- 日志判读：`Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；含 `FAILED` 的行须逐条确认是 PASSED 用例名子串。


## 7. 残留（每条带三件）

- **R1（P2 登记）** 打印槽守卫若写在 `if (…) { … }` 之外用提前返回（`if (ok) return` 反向形态）仍按「无守卫」红。
  触发条件：有人把守卫重构成早退形式 → 门会红，才需要扩判据（正向 fail-closed，不静默）。
  触发后方案：`_if_statements` 增加「早退守卫」识别（条件里带身份比较且在 print 之前 `return`）。
  搁置理由：真树无此写法，现在扩判据只增加误伤面。
- **R2（P2 登记）** 键值判据要求纯引用链，`item.file.absolutePath.hashCode()` 这类「派生但仍跟踪文件」的写法会红。
  触发条件：首次出现合法派生键 → 门红。
  触发后方案：把 `KEY_REF_RE` 扩成「链 + 白名单纯函数调用」。
  搁置理由：真树唯一键位是 `item.file`，扩白名单等于给未来的自己留后门。
- **R3（P2 登记）** `readAtMost` 的 cap 判据限定在 `readSafBytes` 体内；若将来出现第二个读取入口（另一函数）不受此判据约束。
  触发条件：新增第二个 SAF 读取入口。
  触发后方案：把「入口派生」做成 `check_saf_imports` 的通用入口（按「读 args 路径 ∧ 返回 ByteArray」派生函数集），而不是写死函数名。
  搁置理由：当前只有一个入口，派生逻辑的误判面大于收益。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-08-file-preview
    python3 scripts/scan/filepreview_print_bounded_check.py .
    python3 scripts/scan/filepreview_print_bounded_check.py --selftest
    python3 probe/reattack_wide08.py        # 15 条见证全量反攻（本支五门）
    bash scripts/scan/scan.sh               # 112 passed, 0 failed
    python3 scripts/scan/test_scan.py       # ALL 344 CASES PASS

见证夹具本体就在门文件里（`BAD_*` / `BREAKER_*` 常量），把对应 `find` 串换成 `replace` 串即可在真树上重放同一条攻击。
