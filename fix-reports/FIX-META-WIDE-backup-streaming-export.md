# FIX-META-WIDE — backup_streaming_export_check（wide-1024）

分支：`fix/meta-wide-10-skill-backup-compact` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-10-skill-backup-compact`
基线 sha：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
生产代码改动：**0 行**（只改本门脚本）

## 1. 门

`scripts/scan/backup_streaming_export_check.py`（备份链路 6 arms：流式导出 / 轮转 / 拉取落盘 / IO 线程 / 上传守卫 / 忙闸复位）。

## 2. 轴

| 见证 | 轴 | 旧判据为何瞎 |
|---|---|---|
| arm3 消毒结果不被消费 | dataflow | 只做子串 `WebDavSync.sanitizeAutoBackupStamp(` 全文件存在性检查；既不验证 null 拒绝分支，也不验证落盘路径拼的是消毒后的值 |
| arm1 轮转被死条件包裹 | dataflow | 判据在**整个文件**上找 `rotateLocalAutoBackups(app)` 子串；`if (false) rotateLocalAutoBackups(app)` 完全匹配 |
| arm6 finally 末次写回 true | order | 判据只要求 `operationBusy = false` 出现在 finally 块里；其后追加一行 `= true` 仍匹配，而**末次赋值**才是生效值 |

## 3. 见证（外部审计原样）

1. `BackupSettingsScreen.kt`：删掉 `if (stamp == null) { … throw … }`，并把 `File(dir, PREFIX + stamp + ".json")` 换成 `+ rawStamp +`
   —— WebDAV 服务器返回被污染的文件名（`minis-auto-../../../../files/evil.json`）时，消毒器「拒绝」的结果被无视，未消毒值直接进本地路径并 `writeText`（路径穿越写文件复活）。
2. `AutoBackupManager.kt`：`rotateLocalAutoBackups(app)` → `if (false) rotateLocalAutoBackups(app)`
   —— 定时自动备份只增不减，`filesDir/backup-autos` 随天数无限增长直到占满设备。
3. `BackupSettingsScreen.kt`：finally 里在 `operationBusy = false` 之后再写 `operationBusy = true`
   —— 末次写生效，页面所有备份/恢复/导出按钮永久 disabled，直到退出重进。

修复前逐条打补丁实测：**rc=0（门瞎）**。

## 4. 修法

- **arm1 → 作用域派生 + 死守卫判定**：新增 `fun_span()` 从树里定位 `runLocked` 的函数体，三条判据（流式 API、无整文档 API、共享轮转）全部改成**函数体内**判定；再用 `dead_guarded()` 检查调用点自身语句前缀、跨行 `if (...)` 语句头、以及每一层包裹块头的条件，命中常量假（`false` / `!true` / `0 != 0`）即红。找不到 `runLocked` → 带 re-point 提示的违规（不是静默跳过）。
- **arm3 → 逐调用点「结果被消费」判据**：对每个 `sanitizeAutoBackupStamp(` 调用点派生
  （1）结果是否绑定到变量（丢弃即红）；
  （2）是否有拒绝分支（`?:` / `== null` / `requireNotNull`），没有即红；
  （3）净化值（含一跳别名 `v = stamp`）是否出现在同一个块内的 `File(…)` 目的地表达式里，否则红。
  括号失配 → fail-closed。
- **arm6 → 末次写赢**：从 finally 块里取 `operationBusy = (true|false)` 序列，**最后一个**必须是 `false`；一条也没有时保留原文案（release 缺失）。

## 5. 四层证据

- **L1**：`python3 scripts/scan/backup_streaming_export_check.py .` → rc=0（真树绿）；
  `… --selftest` → `BACKUP_STREAMING_EXPORT_SELFTEST=PASS`，既有 14 组夹具行为不变。
- **L2 反攻**：C1 / C2 / C3 → 修复后 rc=1，`restore=True`；修复前同补丁 rc=0。
  门给出的红行分别是 `arm3: the sanitizer's rejection is IGNORED …`、`arm1: runLocked's shared rotation is DEAD-GUARDED …`、`arm6: … the last write on the exit path wins …`。
- **L3 邻门**：`compact_entry_pin_migration_check` rc=0、`backup_remap_rollup_check` rc=0。
- **L4 全量**：`GATE_RUN mode=full ran=112 pass=112 fail=0 sandbox=0 verdict=GREEN`；`test_scan.py` → `✅ ALL 344 CASES PASS`。

自证臂（`run_fixture` 新增 `contains=` 断言，保证**恰好**命中被判据）：
- `VIOLATION_ROTATION_DEAD_GUARD`（`if (false) rotate…`）→ `DEAD-GUARDED`
- `VIOLATION_FETCH_SANITIZER_DEAD`（删拒绝分支 + 用原始名落盘）→ `rejection is IGNORED`
- `VIOLATION_FETCH_RAW_PATH`（保留拒绝分支但落盘用原始名）→ `never read`
- `VIOLATION_FETCH_GATE_RECLAIM`（finally 末次写 `true`）→ `last write`

原有「攻击被挡住」臂（arm4 anchor）未动、仍红。

## 6. CI

见回报行（分支 CI 绿后回填 run id / head_sha）。

## 7. 残留（三件套）

- **R1 动态早退**：arm1 只判「常量假守卫」。`if (skipRotation) return@runLocked` 这类**运行期**提前返回仍会跳过轮转。
  触发条件：真树出现运行期早退跳过 rotate。触发后方案：把 arm1 升级成「rotate 调用在 runLocked 的直达路径上」（控制流图最小版：禁止其前出现 `return` / `throw` / `break`）。搁置理由：需要函数级控制流，静态成本明显高于当前收益。
- **R2 两跳别名**：arm3 只跟踪一跳（`v = stamp`）。`a = stamp; b = a; File(… + b)` 会红（fail-closed，方向安全但不精确）。
  触发条件：出现两跳别名写法且门误红。触发后方案：别名集迭代到不动点（上限 3 跳）。搁置理由：现无实例，误红也只是「要求人看一眼」。
- **R3 arm6 只在 finally 块内判末次写**：若有人把复位搬到 `DisposableEffect`/`try` 尾部，`finally` 判据会报 release 缺失（fail-closed 红）。
  触发条件：出现该重构。触发后方案：把「退出路径」派生成 finally + 所有 suspend 包装的 catch 块。搁置理由：现无实例。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-10-skill-backup-compact
    python3 scripts/scan/backup_streaming_export_check.py .            # rc=0
    python3 scripts/scan/backup_streaming_export_check.py --selftest   # ...SELFTEST=PASS
    python3 /var/minis/shared/work/meta-wide/probe-10/run.py . C1 C2 C3 # 三条 rc=1 restore=True
