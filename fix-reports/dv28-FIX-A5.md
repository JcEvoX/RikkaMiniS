# dv28-FIX-A5 — 恢复流程 Stage 9 覆盖 Stage 7 的 MCP 合并结果（A-05，P1）

- worktree：`/var/minis/shared/work/deep-audit-1048/a5/`
- 分支：`fix/dv28-a5-mcp-restore-stage9`
- 基线：`eea9f407`（`merge: integration/dv27 — deep-audit-1047`）
- 工单：A-05（P1，数据丢失面）；独占生产面 `backup/ConfigBackup.kt`；独占门 `scripts/scan/mcp_oauth_lifecycle_check.py`

---

## ① 缺陷与真树取证

### 1.1 同一份文件被两个阶段写，后写覆盖前写（真树行号，本次实测）

| 阶段 | 位置（本支 HEAD 实测） | 行为 |
|---|---|---|
| 导出 | `ConfigBackup.kt:493-524` | `artifactRoots` 里 `root.name == "mcp-servers"` ⇒ 允许 `json/jsonl/md`，前缀 `"mcp"`，经 `ArtifactArchiver.writeEntries(…, "mcp/", …)` 归档 ⇒ 归档里必有 `mcp/servers.json` |
| Stage 7 恢复 | `:1413-1447`，合并调用 `:1427 mcpRepo.importJSON(srv.toString())` | `MCPRepository.importJSON`（`MCPRepository.kt:571`）是**合并**语义：`_servers.value.filter { it.id !in incomingIds } + parsed`，随后 `save()` 落盘 ⇒ 本机独有的服务器被保留 |
| Stage 9 恢复 | `:1524-1562`，解压 `:1562 ArtifactArchiver.extractZipByPrefix(filtered, destByPrefix)` | `destByPrefix["mcp"] = mcp-servers root`；`extractZipByPrefix`（`ArtifactArchiver.kt:95-130`）对每个 entry `target.outputStream().use { input.copyTo(it) }` **整文件覆盖** ⇒ `mcp/servers.json` 把 Stage 7 刚写好的合并版换成备份原版 |

结果：**目标设备本地的 MCP 服务器被静默抹除**；`ConfigBackup` 全文 `reloadFromDisk` 只有 1 处（`:1379`，`skillRepo`）⇒ 内存里 `_servers` 还是合并版，磁盘已是备份版，**内存/磁盘分裂直到下一次冷启动读盘才显现**。

### 1.2 本次跑出的实证（不是报告转述）

L3 装置把基线 `stripMcpOAuthEntries`（`git show eea9f407:…`）切出来真跑 JVM（`probe/l3/arms/pre/junit.log`）：

```
1) extractionLeavesTheMergedServerListOnDisk(…PreTest)
   org.junit.ComparisonFailure: the merged list must survive the artifact stage
   expected:<[MERGED]> but was:<[BACKUP]>
2) stage7MergedDropsTheServerListFromTheRestoreStream(…PreTest)
   java.lang.AssertionError: the archive's mcp/servers.json must not reach the extraction
Tests run: 2,  Failures: 2
```

`BACKUP` 覆盖 `MERGED` = 缺陷本体在真实 `java.util.zip` 字节流上复现。

### 1.3 「先证」：现有测试里谁会被这次过滤影响

`grep -rln "extractZipByPrefix\|stripMcpOAuthEntries\|importJSON" src/android/app/src/test/` ⇒ 只有 `ArtifactBackupScopeTest.kt`。
其中 `stage9FilterKeepsRestoreFromClobberingTheLiveBridge`（`:340-380`）**不受影响**：它调的是两参重载（新参数默认 `false`），断言 OCR 计数 2 与 `servers.json == "S"` 仍成立——本支**没有改动它的任何一行断言**，只把返回值改成读 `.oauthDropped` / `.dropped`（返回值类型从 `Int` 变成结果对象）。

---

## ② 修法（改动阶梯）

**停在第 1 级：排除一个 entry，复用既有过滤点。** 理由：Stage 9 已经有「解压前先过滤归档」这一步（`[da1034-A1]` 的凭证墙，`:1544`），`mcp/servers.json` 要做的正是同一件事——**从抽取流里去掉一个 entry**，不需要新阶段、不需要碰 `MCPRepository`。

比任务书第 1 级多出的一处是**开关**（任务书未要求，属实现必需的边界）：无条件排除会让「没有 `mcpServers` 段的纯归档备份」（hand-crafted / archive-only）**彻底丢失服务器列表**——那种备份里 Stage 7 从未运行，解压是唯一载体。所以：

```kotlin
// Stage 7 之前，从「合并到底跑没跑」的两个操作数派生
val serverListMergedInStage7 = mcpRepo != null && mcpArr != null && mcpArr.length() > 0
…
// Stage 9
val filterResult = stripMcpOAuthEntries(tmp, filtered,
    dropPrestagedServerList = serverListMergedInStage7)
```

- 合并跑了 ⇒ 丢弃归档里的 `mcp/servers.json`，并 `skipped += "artifacts: servers.json restored via Stage 7 merge (skipped raw copy)"`；
- 合并没跑（没有该段 / 空数组 / 无修行仓库）⇒ **原样解压**，旧备份仍能恢复（任务书「必须保住」的第三条）。

排除**精确到文件名**：`isMcpServerListEntryName` 只认 `mcp/servers.json`（空段与 `"."` 段折叠后逐段比较，`mcp//servers.json`、`mcp/./servers.json` 同样识别为同一目标），`mcp/` 下其它文件（agent 写的自定义数据）照常恢复。

**必须保住的四条，逐条核对**：
1. Stage 7 合并语义与 `skipped` 文案——未改一行；
2. `[da1034-A1]` 的 oauth 过滤——同一函数里先判 oauth 再判 server list，两计数独立；L3 有专门的「开关两种取值下 oauth 条目都必丢」用例；
3. `extractZipByPrefix` 的 tar-slip 守卫与 `isUnderBase` 边界——未触碰（L3 直接跑真实 `ArtifactArchiver` 切片，含 `isUnderBase`）；
4. 旧备份恢复仍成功——`dropPrestagedServerList = false` 路径由 `archiveOnlyBackupStillRestoresTheServerList` 钉住。

**不得引入的三条**：没删 `importJSON` 的 `save()`；没把 Stage 7 改成覆盖式导入；没动 `MCPRepository.kt`（零改动）。

---

## ③ 改动清单（3 文件 +552/−27，另有报告）

| 文件 | 改动 |
|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/backup/ConfigBackup.kt` | +100/−13：Stage 7 前派生 `serverListMergedInStage7`（`:1417-1425`）；Stage 9 传开关 + 两条 `skipped`（`:1544-1560`）；`stripMcpOAuthEntries` 增第三参 + 双计数返回值 + `RestoreEntryFilterResult`（`:1819-1879`）；新纯函数 `isMcpServerListEntryName`（`:1884-1898`） |
| `src/android/app/src/test/java/com/rikkaminis/app/backup/ArtifactBackupScopeTest.kt` | +81/−2：既有两处断言改读 `.oauthDropped` / `.dropped`（语义不变）；新增 `serverListEntryNamePinsOnlyTheOneFileUnderTheMcpRoot`、`stage9SkipsTheServerListOnlyWhenStage7MergedIt`（含「合并后解压不得覆盖本地 servers.json」的端到端断言） |
| `scripts/scan/mcp_oauth_lifecycle_check.py` | +398/−8：新增 `check_stage9_premerged`（C-ST9 + C-ST9b）、派生助手（`_match_paren` / `_top_level_args` / `_enclosing_guard_identifiers`）、7 条带牙臂 + 1 合规臂、文档段；A2 的调用形状正则放宽到容忍「一个尾部具名实参」（输出仍是第 2 个位置/`dst` 参数，牙齿不变） |
| `fix-reports/dv28-FIX-A5.md` | 本报告 |

`scan.sh` / `test_scan.py` 零触碰；**门 111 / 夹具 342 不变**（0 新建门）。

---

## ④ 门与判据（扩 `mcp_oauth_lifecycle_check.py`）

### C-ST9（同一文件不得被两阶段写）
全部**派生**，无行号、无成员清单：

1. `import` 里存在 `mcpRepo.importJSON(` ⇒ 合并阶段存在；不存在则本判据沉默（没有第二个写者）。
2. 过滤调用必须带**第三个实参**，且该实参是**标识符**：
   - `true` / `false` / 任何非标识符 ⇒ 红（「字面量不是决策」：无条件丢弃会丢 archive-only 备份的列表）；
   - 该标识符必须在 `import` 内用 `val/var` 计算过，且右侧表达式命中的标识符与「Stage 7 合并调用所在 `if (…)` 的条件标识符集」**至少交 2 个** ⇒ 从合并守卫派生，改名不失明、写死 `true` 会红。
3. 同文件内必须存在一个**谓词**，其函数体同时钉 `"mcp"` 与该文件名；过滤函数必须在**同一个条件**里调用它（`dropPrestagedServerList && isMcpServerListEntryName(…)`），缺开关 ⇒ 红（等于无条件丢弃）。
4. `"mcp"` 仍必须是解压目的地之一（`destByPrefix` 的前缀字面量）⇒ 防「顺手把整个前缀丢掉」这个更坏的「修法」。

### C-ST9b（磁盘一致性动作或声明，白名单两种形态）
- (a) 解压**之后**再次 `importJSON(` 且 `reloadFromDisk()` 位于其后；或
- (b) 同函数内存在一个字符串字面量同时含该文件名与「Stage 7 merge」。
- 注释不算：结构半边用 `mask()`（字符串/注释全抹白），声明半边用 `mask_comments()`（只抹注释），因此「只加注释」的臂在 C-ST9 上仍然是红的。

### 带牙臂（`--selftest` 输出，每臂**恰好 1 条**违规）
`selftest dv28-a5 stage9 arms (no-drop/comment-only/unconditional/flag-ignores-guard/hollow-pred/whole-prefix/undeclared) -> 1/1/1/1/1/1/1 violation(s)`，另有合规臂 0 违规。

| 臂 | 构造 | 命中判据 |
|---|---|---|
| no-drop | 过滤函数不含 server-list 分支 | C-ST9.3 |
| comment-only | 分支换成一句注释 | C-ST9.3（注释不满足） |
| unconditional | 开关传 `true` | C-ST9.2 字面量 |
| flag-ignores-guard | `val flag = true` | C-ST9.2 派生 |
| hollow-pred | 谓词钉 `other.json` | C-ST9.3 谓词 |
| whole-prefix | 解压目标不含 `mcp` | C-ST9.4 |
| undeclared | 删掉 `skipped` 文案且无 reload | C-ST9b |

### PRE 臂
`probe/pre/tree/`（8 个门依赖文件，其中 `ConfigBackup.kt` 换成 `git show eea9f407:…`）⇒ 门 **rc=1，恰 1 条**：

```
ConfigBackup.kt: `stripMcpOAuthEntries(` does not learn that Stage 7 already merged this
backup's server list — the archive's `servers.json` is still written verbatim over the
merged file, so every server this device has and the backup does not is erased …
```

---

## ⑤ 验证四层

### L1 门层
- 本门真树：`PASS mcp_oauth_lifecycle_check …; the server list Stage 7 merged is never re-copied over by the artifact stage`，`rc=0`
- `--selftest`：`MCP_OAUTH_LIFECYCLE_SELFTEST=PASS`（含新增 7 臂，各自 1 条违规）
- PRE 臂：rc=1，恰 1 条（见 ④）
- 全量 `sh scripts/scan/scan.sh`：`║  Summary: 111 passed, 0 failed`，门头 1..111 互异、连续（111 条），`Function not implemented` 0 次（`probe/scan-1.log`）
- `python3 scripts/scan/test_scan.py`：`✅ ALL 342 CASES PASS`，rc=0（`probe/test_scan.log`）

> ENOSYS 风暴露过一次：前三次整链跑分别在第 2/15 门被 `python3: Function not implemented` 打断（`Summary: 25 passed, 86 failed` 之类**全是假红**）。按 registry S28 处理：`probe/scan_retry.sh` 循环重跑到「rc=0 且 ENOSYS 计数为 0」为止，第 1 次即绿。假红未被写成真红。

### L2 邻门层（8/8 rc=0）
`grep -rl "ConfigBackup" scripts/scan/*.py` 得 7 道真邻门，加 1 道凭证写入门：

```
0 backup_remap_rollup_check      0 backup_streaming_export_check
0 envvar_config_group_check      0 openminis_compat_field_parity_check
0 parts_json_funnel_check        0 provider_ua_parity_check
0 offload_cli_args_check         0 readonly_mount_write_guard
```
（`mcp_oauth_lifecycle_check` 本身见 L1。）

### L3 装置层（真 `java.util.zip` + 真 kotlinc 2.0.21 + JUnitCore）
装置 `probe/l3/`：`extract.py` 从真文件按花括号切出**逐字节**声明（唯一合成件 = `PRootKernel` 桩，只回答「能不能写」，所有 operand 都是临时文件 ⇒ 恒 `false`），`run_arm.sh` 每臂独立进程（COMMON §7）。

| 臂 | 结果 |
|---|---|
| **POST**（真支切片，`PostTest` 5 用例） | `OK (5 tests)` |
| **PRE**（`eea9f407` 切片，2 用例） | **红 2/2**：`expected:<MERGED> but was:<BACKUP>`、`must not reach the extraction` |
| m1 开关被忽略（`dropPrestagedServerList && pred` → `false`） | 红 2/5：`stage7MergedDrops…`、`extractionLeaves…`（archive-only 与谓词臂仍绿 = 半径精确） |
| m2 谓词过宽（`segs.size >= 1 && segs[0] == "mcp"`） | 红 3/5：谓词臂 + 两条 server-list 臂（`mcp/other.json` 被误删） |
| m3 凭证墙拆除（删 oauth 分支） | 红 4/5：三条观察 oauth 的用例 + 两条 server-list（`oauth/token.json` 复活）；谓词臂仍绿 |
| **repo-test 臂**（真 `ArtifactBackupScope.kt` + 真 `ArtifactArchiver.kt` + 真 `ArtifactBackupScopeTest.kt` + 切片的 `ConfigBackup`） | 编译 rc=0；`OK (13 tests)` —— 即 CI 要跑的测试代码在本地就以真逻辑跑过一遍 |

还原纪律：变异只施加在 `probe/l3/arms/*` 的切片副本上，生产文件全程未进入变异态（`git status` 只有 3 个预期文件），故无需「还原 + sha256 对拍」；`arms/post/ConfigBackupSlice.kt` 由 `extract.py` 从工作树重生，内容 sha256 稳定。

### L4 CI 层
见 §⑦（CI 绿后回填）。

---

## ⑥ 残留与失真

| 编号 | 内容 | 级别/触发条件 |
|---|---|---|
| R1 | **真机/真备份端到端**：造「本机有 A、备份只有 B」→ 恢复后 A+B 都在，且**冷启动后**仍在（内存/磁盘分裂是本缺陷的伪装层，必须冷启动验证） | 真机窗口 |
| R2 | Stage 5（`mcpServers` 段）与 Stage 7 是否重复导入同一份列表（幂等性）：Stage 5 走 `MCPRepository` 的配置段，Stage 7 走 `importJSON`；两者都跑时 `importJSON` 的「按 id 过滤后追加」是幂等的，但**未实测**同一备份里两段同时存在的情形 | 触发条件 = 出现「同 id 重复条目」的用户报告或备份样本 |
| R3 | 其它 artifact 面是否有「两阶段写同一文件」：`shared/` 下的路径若同时被 Stage 10 写，则同样的覆盖语义会复发（本支已用派生判据覆盖 `mcp/servers.json` 一个文件名，不覆盖 `shared/`） | 触发条件 = 新增「某 Stage 既合并又整文件解压」的路径 |
| R4 | **未来新增 MCP 附属文件不得与 `servers.json` 同名**：谓词按文件名钉住，若以后 `MCPRepository` 换写盘名（如 `servers-v2.json`），排除会静默失效（门会红，因为谓词钉的字面量仍在但新名无人排除——需要同时改谓词与门） | 触发条件 = `MCPRepository` 改 `servers.json` 文件名 |
| R5 | 门 C-ST9.2 的「至少交 2 个标识符」是启发式：若有人把守卫改写成 `if (!canMerge(mcpRepo))` 这种单标识符形态，判据会红（fail-closed，需重锚），不是漏判 | 触发条件 = Stage 7 守卫形态变更 |
| R6 | C-ST9b 的白名单 (a) 只要求「解压后有 importJSON 且 reloadFromDisk 在其后」，不校验对象是 MCP 仓库（同文件里还有 `skillRepo.reloadFromDisk`）——判据已绑定「位置在解压之后」，但未绑定接收者 | 触发条件 = 出现第二个「解压后 reload」的仓库 |
| R7 | 报告与任务书的偏差：任务书 §5 L3 要求「两个内存 zip」；本支用**一个含 4 个 entry 的 zip + 一个只含 `mcp/oauth/token.json` 的 zip**（`oauthBridgeEntriesAreDroppedUnderBothSwitchValues`），断言集合与任务书等价 | 记账项，无需修 |

**失真/冲突记录**：任务书正文与真树一致（`:493-524` / `:1413-1447` / `:1500-1542` 分段准确，行号有 ±10 漂移属正常）；未发现需要驳回的表述。

---

## ⑦ 身份链

- 基线：`eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`
- 分支：`fix/dv28-a5-mcp-restore-stage9`
- 提交：见文件末尾「提交序列」（CI 绿后回填 run/job/head_sha 三方一致）

---

## ⑧ 自证成本与不确定项

- **装置自伤三处（都已闭环，形态可复用）**：
  1. `_enclosing_guard_identifiers` 首版只判「条件的 `)` 在合并调用之后」⇒ 命中了 `import()` **后面**的 `if (onScheduledTasks != null)`（`:1614`），把守卫操作数算成 `{onScheduledTasks}`，真树当场假红（互相交不到 2 个）。修法 = 「enclosing」必须同时要求 `if` 之后的花括号块**跨越**调用点。**教训：判「谁包着谁」要判块边界，不是判条件位置。**
  2. C-ST9b 首版只判「`import` 里出现过 `reloadFromDisk(`」⇒ 撞上 Stage 5 的 `skillRepo.reloadFromDisk()`（`:1379`，在解压之前），于是把合规实现判成「reload 在解压前」。修法 = 白名单形态必须**绑定位置**（解压之后）。**教训：同函数里同名的调用可能有完全不同的用途，判据要绑定位置/接收者，不能只判存在。**
  3. `extract.py` 的 `decl()` 忘了把 `kind` 传下去（`data class` 被当 `fun` 找）⇒ 「declaration not found」；另 `isUnderBase` 是**表达式体**，首版按花括号切直接失败。**教训：切片器必须同时认「块体」与「表达式体」两种声明形态。**
- **不确定项**：CI 才能给出的证据是「真 Gradle 编译 + 单测在 android.jar 下通过」；本地已用真 kotlinc 编译真测试文件（repo-test 臂 13/13），但仍不能替代 CI 的 `compileReleaseKotlin`。
- **成本**：约 3.5 小时（含 3 次 ENOSYS 风暴等待与 2 次装置返工）；门改动 398 行中约 1/3 是新的派生助手（`_match_paren` / `_top_level_args` / `_enclosing_guard_identifiers`），其余是判据、文档与 8 条臂。
