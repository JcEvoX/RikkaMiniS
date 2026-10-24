# fastio_tar_bounds_check — fastio tar/copy 边界（wide-1024）

> wide-1024 门见证加固 · 分支 `fix/meta-wide-08-file-preview` · 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
> 门文件 `scripts/scan/fastio_tar_bounds_check.py` · sha256 前 16 位 `1830537cddc77583`
> 本波**零生产代码改动**：五个 `.kt` 生产文件首字节未变（见 §5 L2 的 restore 记录）。

## 1. 门

`fastio_tar_bounds_check.py` — 钉住 `sandbox/offload/FastioTar.kt` 的条目预算与失败补零、`FastioCopy.kt` / `FastioSearch.kt` 的特殊文件守卫。

自证标记（冻结面 `test_scan.py` 全等断言，**一字未改**）：`FASTIO_TAR_BOUNDS_SELFTEST=PASS`

## 2. 轴

**`dataflow`（补零量 / 返回值）+ `order`→liveness（守卫条件）**：

- 补零：写了 0 字节 ≠ 补齐了 header 已声明的 `size`。
- writeTree：消费了 `false` ≠ 停下来了（`if (!writeTree(…)) Unit`）。
- 特殊文件：守卫 token 在正确位置 ≠ 条件活着（`false && !attrs.isRegularFile`）。

## 3. 见证（修复前 rc=0 / 修复后 rc≠0）

| 见证 | 旧判据为何看不见 | 新判据凭什么抓住 |
|---|---|---|
| `catch { runCatching { writeZeros(sink, 0L) } }` | 只查 catch 段里有 `writeZeros(` / `sink.write(`——补零量不看 | 判据看**补零量**：catch 段必须同时出现 `size - written`（补齐未投递载荷）与 `boundaryPad(`（补齐 512 边界）；常量补零判红，并分别说明「流比 header 短 → 后续成员被当 payload 吞掉」「下一个 header 落在非 512 边界 → 归档不可读」 |
| `if (!writeTree(sink, path, name, path, maxEntries, out)) Unit` | 只查 `writeTree(` 前有 `!`（或匹配 `== false`）——极性对，动作被换成空操作 | 在「极性已消费」之上再判**语句是否停环**：取该调用所在语句（同块含 `{`/`}` 时取整块），必须出现 `break`/`return`；否则红（预算耗尽后仍在归档剩余 root） |
| `if (false && !attrs.isRegularFile && !attrs.isSymbolicLink)` | 只查守卫 token 出现在调用之前/实参位置 | 新增 `_enclosing_guard_cond()`：找**条件里包含该 token** 的最近 `if/while`，条件含 `false &&` / `true \|\|` 判红（死守卫）；token 不在任何 if 条件里（如 subject-less `when` 分支）则不套用此判据（见 §7 R1） |

## 4. 修法

1. `check_write_regular`：把「有没有补零」升级成「补的是不是那笔账」——`re.search(r"\bsize\s*-\s*written\b")` + `boundaryPad(` 双判据，常量补零单独红。
2. `check_toplevel_roots`：保留「`!` / `== false` 消费」判据，新增语句级停环判据（`_close_brace()` 支持 `{ break }` 块形态）。
3. `check_special_file_guards`：新增 `_enclosing_guard_cond()` + `SPECIAL_DEAD_GUARD_RE`，把「位置正确」升级成「决定点活着」；解析不出条件时只判位置（保守），不误红。
4. 新增 `_close_paren()` / `_close_brace()` / `_enclosing_guard_cond()` 三个括号感知工具，全部在解析失败处走保守/fail-closed 分支并在消息里写明。

设计约束（本波五门共用）：
- **解析失败 fail-closed**：括号/花括号失配一律报违规，消息里点明 `fail-closed`，绝不静默返回绿。
- **锚决定点，不锚拼写**：判据读的是「谁决定了这个值/这条控制流」，不是「文件里有没有这个词」。
- **不误伤合法写法**：真树必须仍绿；只把判据放宽到能过真树、同时仍抓住见证的那一档。

## 5. 四层证据

### L1 本门

    python3 scripts/scan/fastio_tar_bounds_check.py .        -> rc=0（真树绿）
    python3 scripts/scan/fastio_tar_bounds_check.py --selftest -> FASTIO_TAR_BOUNDS_SELFTEST=PASS

新增/改动臂（fixture 断言「恰好红对应判据」）：

    constant-refill fixture (wide-1024)      （新）`writeZeros(sink, 0L)`            → 红 "UNDELIVERED payload"
    unit-branch fixture (wide-1024)         （新）`if (!writeTree(…)) Unit`           → 红 "does not STOP on it"
    short-circuited copy guard (wide-1024)  （新）`if (false && !attrs.isRegularFile…)` → 红 "short-circuited"（恰好一条）
    原有写点/预算/位置家族臂全部保持原判

### L2 见证反攻（唯一有效证据）

`probe/reattack_wide08.py`：对每条见证「打补丁 → 跑本门 → 必须 rc≠0 → 恢复 → sha256 比对」。

    check_write_regular constant refill      rc=1 restore=True
check_toplevel_roots Unit branch         rc=1 restore=True
check_special_file_guards dead guard     rc=1 restore=True

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

- **R1（P2 登记）** `findTree` 的守卫写成 subject-less `when { attrs.isRegularFile -> … }`，没有 `if (…)` 条件可判，死守卫判据不套用（只保留位置判据）。若有人把 when 分支改成 `false && attrs.isRegularFile ->` 仍绿。
  触发条件：出现 when 分支被短路打穿的见证。
  触发后方案：解析 `when { … }` 分支条件（`->` 左侧表达式），套用同一 `DEAD_GUARD_RE`。
  搁置理由：本波三条见证都打在 `if` 条件上；扩到 when 需要额外解析分支边界，收益未知。
- **R2（P2 登记）** 补零量判据锚「`size - written` + `boundaryPad(`」两个表达式；若补零改写为 `writeZeros(sink, remaining)`（把 `size - written` 先绑到局部变量），会误红。
  触发条件：改写为局部变量形态 → 门红。
  触发后方案：接受「被绑定到局部变量的 size-written」：在 catch 段内做一层定义-使用追踪。
  搁置理由：真树是直传形态；扩定义-使用追踪会让判据从 40 行涨到 200 行，装置比例不合适。
- **R3（P2 登记）** `check_toplevel_roots` 的停环判据看「调用所在语句/块里有没有 break|return」；若循环体里同时还有其他 `break`（例如错误分支），即使 `!writeTree` 的分支是空操作也可能蒙混。
  触发条件：出现同类见证。
  触发后方案：定位 `if (!writeTree(…))` 的 **then 体**（花括号感知）再判停环，而不是看整条语句/整块。
  搁置理由：真树 then 体只有 `break` 一条语句，语句级已经足够；`_close_brace` 的 then 体定位已具备，触发时十行内可升级。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-08-file-preview
    python3 scripts/scan/fastio_tar_bounds_check.py .
    python3 scripts/scan/fastio_tar_bounds_check.py --selftest
    python3 probe/reattack_wide08.py        # 15 条见证全量反攻（本支五门）
    bash scripts/scan/scan.sh               # 112 passed, 0 failed
    python3 scripts/scan/test_scan.py       # ALL 344 CASES PASS

见证夹具本体就在门文件里（`BAD_*` / `BREAKER_*` 常量），把对应 `find` 串换成 `replace` 串即可在真树上重放同一条攻击。
