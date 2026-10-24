# share_dir_prune_check — cacheDir/share 回收与分享交接（wide-1024）

> wide-1024 门见证加固 · 分支 `fix/meta-wide-08-file-preview` · 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
> 门文件 `scripts/scan/share_dir_prune_check.py` · sha256 前 16 位 `44671e93ad8d8aac`
> 本波**零生产代码改动**：五个 `.kt` 生产文件首字节未变（见 §5 L2 的 restore 记录）。

## 1. 门

`share_dir_prune_check.py` — 钉住三件事：每个写 `cacheDir/share` 的点都调共享 pruner；pruner 拿到的确实是那个 share 目录；分享交接的返回值真的来自启动结果。

自证标记（冻结面 `test_scan.py` 全等断言，**一字未改**）：`SHARE_DIR_PRUNE_SELFTEST=PASS（含子标记 CRASH_SHARE / ARM5_FILESDIR_PRUNE / SHARE_HANDOFF / SHARE_HANDOFF_DERIVED）`

## 2. 轴

**`dataflow`（arm2 / arm6）+ `dataflow`→参数的语义（arm5）**：

- arm2：调用了 `pruneShareDir(` ≠ 参数是那棵刚建的目录树。
- arm5：调用了共享 pruner ≠ 用的是共享 TTL（`maxAgeMs = Long.MAX_VALUE` 让 `cutoff` 永远晚于任何文件，横扫变空转）。
- arm6：`runCatching` 包住了 `startActivity` ≠ 返回值来自它（`.let { true }` 让失败照样报成功）。

## 3. 见证（修复前 rc=0 / 修复后 rc≠0）

| 见证 | 旧判据为何看不见 | 新判据凭什么抓住 |
|---|---|---|
| arm2 `pruneShareDir(File(context.cacheDir, "noop"))` | 只查「`File(context.cacheDir, \"share\")` 出现在 pruner 之前」+ mkdirs 同块——顺序全对，只是剪错目录 | 解析 `val <name> = File(context.cacheDir, "share")` 拿到变量名，再解析 `pruneShareDir(...)` 的首个实参：必须是该变量（或同一字面量构造）；找不到绑定 → fail-closed 红 |
| arm5 `pruneShareDir(dir, maxAgeMs = Long.MAX_VALUE)` | 只查 `pruneShareDir(` 出现 | 新增 `prune_ttl_problem()`：每个 `pruneShareDir` 调用的第 2+ 个实参只允许**全大写下划线常量名**，其余（`Long.MAX_VALUE` / 数字表达式）一律红；同时挂到 4 个写点 + `pruneStaleAttachments` 委派点 |
| arm6 `return runCatching { context.startActivity(chooser) }.let { true }` | 只查「`startActivity` 被 `runCatching`/`try` 包住」 | 解析 `runCatching { … }` 的块尾，其后必须接 `VERDICT_CONSUME_RE`（`.isSuccess/.isFailure/.fold/.getOrElse/.getOrNull/.exceptionOrNull/.onSuccess/.onFailure/?::`）才认；`.let { … }` 或无消费者 → 红 |

## 4. 修法

1. 新增 `split_call_args()`（顶层逗号切分、括号感知、读不出返回 `None`），arm2 与 arm5 共用。
2. arm2 补「目录身份」判据：把 pruner 的实参与上一行构造出的 share 目录变量比对。
3. 新增 `prune_ttl_problem()`，`PRUNE_TTL_CONST_RE = [A-Z][A-Z0-9_]*`：只允许具名常量覆盖 TTL，把「数字要带着理由走」变成机械可判。挂在 arm5 的写点循环**和** `pruneStaleAttachments` 委派分支（同族两处，一次修共享层）。
4. arm6 新增 `runCatching }` 之后的消费判据：`VERDICT_CONSUME_RE` 之外一律红（值丢弃 = 失败照样报成功）。

设计约束（本波五门共用）：
- **解析失败 fail-closed**：括号/花括号失配一律报违规，消息里点明 `fail-closed`，绝不静默返回绿。
- **锚决定点，不锚拼写**：判据读的是「谁决定了这个值/这条控制流」，不是「文件里有没有这个词」。
- **不误伤合法写法**：真树必须仍绿；只把判据放宽到能过真树、同时仍抓住见证的那一档。

## 5. 四层证据

### L1 本门

    python3 scripts/scan/share_dir_prune_check.py .        -> rc=0（真树绿）
    python3 scripts/scan/share_dir_prune_check.py --selftest -> SHARE_DIR_PRUNE_SELFTEST=PASS（含子标记 CRASH_SHARE / ARM5_FILESDIR_PRUNE / SHARE_HANDOFF / SHARE_HANDOFF_DERIVED）

新增/改动臂（fixture 断言「恰好红对应判据」）：

    arm2 violation-wrong-prune-dir        （新）剪错目录                      → 红
    arm5 arm5-unbounded-ttl-at-mutation   （新）委派点 maxAgeMs=MAX_VALUE     → 红
    arm5 arm5-unbounded-ttl-at-writer     （新）写点 maxAgeMs=MAX_VALUE       → 红（证明挂在共享层）
    arm6 b04-verdict replaced by a constant（新）`.let { true }`              → 红
    原有 no-prune / allowlist / handoff 家族臂全部保持原判

### L2 见证反攻（唯一有效证据）

`probe/reattack_wide08.py`：对每条见证「打补丁 → 跑本门 → 必须 rc≠0 → 恢复 → sha256 比对」。

    arm2 prunes a directory that is not the share dir   rc=1 restore=True
arm6 C-SILENT verdict replaced by a constant        rc=1 restore=True
arm5 prune TTL made unbounded                       rc=1 restore=True

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

- **R1（P2 登记）** arm6 只判「`runCatching` 的结果被谁消费」，不判「消费后是否上报」——`.isSuccess` 拿到手却丢弃（`runCatching{…}.isSuccess` 单独一行）仍绿（这一条原本就由 arm3 的 feedback 判据负责，此处只兜住「换个恒真值」）。
  触发条件：出现「消费了但没用」的新形态。
  触发后方案：把 `VERDICT_CONSUME_RE` 升级为「结果必须绑定到返回值或显式分支」，需做表达式级数据流。
  搁置理由：与 arm3 判据重叠，重复装置的边际收益低。
- **R2（P2 登记）** arm2 的「目录身份」判据只认 `val <name> = File(context.cacheDir, "share")` 这一种构造形态；若改写为 `File(requireNotNull(context.cacheDir), "share")` 或经 `cacheDirFile(dir, "share")` 辅助函数构造 → 找不到绑定 → fail-closed 红（不静默放过，但会红一次）。
  触发条件：Stage 的目录构造改写 → 门红。
  触发后方案：把构造识别改成「实参里含 share 字面量的 `File(` 调用」，并接受一层同文件委托。
  搁置理由：真树构造点唯一且形态固定。
- **R3（P2 登记）** `maxAgeMs` 的位置参数形态（`pruneShareDir(dir, Long.MAX_VALUE)`）会被 `split_call_args` 取为第二个实参，判据同样红；但若将来 pruner 签名改成 `(dir, pattern, maxAgeMs)`，第 3 个参数才代表 TTL，正则式判据会把合法的 pattern 当 TTL 误判。
  触发条件：pruner 签名变更。
  触发后方案：按签名派生参数位置（读 `fun pruneShareDir(` 的形参表），不按序号猜。
  搁置理由：签名 `(dir, maxAgeMs)` 稳定，现在做形参派生是提前抽象。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-08-file-preview
    python3 scripts/scan/share_dir_prune_check.py .
    python3 scripts/scan/share_dir_prune_check.py --selftest
    python3 probe/reattack_wide08.py        # 15 条见证全量反攻（本支五门）
    bash scripts/scan/scan.sh               # 112 passed, 0 failed
    python3 scripts/scan/test_scan.py       # ALL 344 CASES PASS

见证夹具本体就在门文件里（`BAD_*` / `BREAKER_*` 常量），把对应 `find` 串换成 `replace` 串即可在真树上重放同一条攻击。
