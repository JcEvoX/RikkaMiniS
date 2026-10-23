# FIX-E8（deep-audit-1046 / S-08，P1）— SCOPE_MOUNT WebApp 冷启动与挂载快照竞态

- 分支：`fix/dv26-e8-webapp-mount-coldstart`
- 基线 main：`f7aba218`
- 生产文件：`webapp/WebAppPathResolver.kt`（改）、`webapp/MountResolvePolicy.kt`（新增）
- 门：扩 `scripts/scan/webapp_asset_url_cleanup_check.py`（未新建门、未触碰 `scan.sh` / `test_scan.py`）
- 装置：`probe/l3/`（真 kotlinc + JUnitCore，POST/PRE/3 变异）、`probe/base/`（`git show f7aba218:` 基线树）

---

## 1. 缺陷与真树取证（基线 f7aba218）

### 1.1 症状链

桌面快捷方式 `Intent` 直接拉起 `WebAppActivity`（manifest `exported="true"`），整条链是一次性、无兜底的读取：

| 环节 | 真树位置（基线） | 事实 |
|---|---|---|
| 路径解析 | `WebAppPathResolver.kt:28-34` | `SCOPE_MOUNT -> PRootKernel.resolveHostPath(shortcut.htmlPath)`，**一次**，无快照兜底 |
| 绑定集合来源 | `PRootKernel.applyMountedFoldersSnapshot` | 唯一的填充点，写 `bindMounts["/var/minis/mounts/<name>"]` |
| 填充时机 | `MinisApp.kt:647-650` | `applicationScope.launch { withContext(Dispatchers.IO) { … } }` —— **异步**（`[dv20-A4-A06]` 为避免 FUSE readdir 卡首帧刻意移到 Main 之外） |
| 消费时机 | `WebAppActivity.kt:183-190` | `withContext(Dispatchers.IO){ resolve(...) }` → `if (file == null) renderSourceMissing(...)`，**无等待、无重试** |

### 1.2 失败机理（读代码可判定，不需要真机）

`resolveHostPath` 的绑定未命中路径（`PRootKernel.kt:1516-1569` `resolveGuestHostPath`）在 `bindMounts` 里找不到 `/var/minis/mounts/<name>` 时，走 `:1563` 的 rootfs 兜底：返回 `<rootfs>/var/minis/mounts/<name>/…`。而 `materializeMountTargets`（`:444-`）为每个挂载点建的**只是一个空目录**。

⇒ 返回的不是 null，而是一个**存在但不存在该文件的路径**；`takeIf { it.exists() && it.isFile }` 判否 ⇒ `null` ⇒ `renderSourceMissing`。也就是说：**快捷方式本身完好、文件就在宿主上，界面却报"来源缺失"，且不可恢复**（同一 Activity 无重试入口）。

### 1.3 为什么这是真缺陷而不是"理论竞态"

- 竞态窗口不是毫秒级：`applyMountedFoldersSnapshot` 要跑 SAF `readdir`（远端 provider 可达数百 ms~数秒）+ rootfs `mkdirs` + 写 9 个 wrapper 脚本（`installMountWriteGuards`）。
- 冷启动是**最常见**的入口形态——从桌面图标点开，没有任何前置的沙箱启动来"顺便"等快照。
- 对照组存在且已修：`PRootKernel.boot`（`:276`）在起沙箱前显式 `withContext(Dispatchers.IO){ applyMountedFoldersSnapshot(context) }` 等就绪。**唯独 WebApp 快捷方式这条快路径前面没有 boot**，也就没有那个等待点。

### 1.4 报告与真树的偏差

任务书行号与真树一致（`:27-34` / `MinisApp.kt:649` / `WebAppActivity.kt:174-196`），仅 `MinisApp` 的异步块实际是 `:647-650`。无机制臆造。

---

## 2. 修法（改动阶梯）

**停在第 6 级（最小实现：一个分支 + 一次幂等重试），因为**：代码库里没有现成的「等快照就绪」原语可复用（`boot` 的等待点是内联的，不是函数）；标准库/平台没提供；加依赖或配置项都不成立；问题的形状就是"一次读 + 一次重跑"。

### 2.1 决策抽成纯函数（`MountResolvePolicy.kt`，Android 无关）

```kotlin
internal fun resolveMountWithRetry(
    htmlPath: String,
    mountKnown: (String) -> Boolean,   // PRootKernel.bindMounts 是否已知该挂载根
    probe: (String) -> String?,        // PRootKernel.resolveHostPath
    usable: (String) -> Boolean,       // exists() && isFile
    refresh: () -> Unit,               // applyMountedFoldersSnapshot
): String?
```

判定：

1. `probe` 落到 `usable` 的路径 → 接受。
2. 不可用 + 快照尚未刷新 + **挂载根不在 bindMounts** → 刷新一次后回到 1（`MOUNT_RESOLVE_MAX_ATTEMPTS = 2` 硬上限）。
3. 不可用 + 挂载根**已知**（文件是真被删了）或已刷新过 → 立即放弃，不付 SAF readdir 的代价。
4. `refresh` 抛异常被 `runCatching` 吞掉 → 仍走"来源缺失"，不会从 Activity 崩出去。

三条设计要点：

- **`mountRootOf` 从 `htmlPath` 派生挂载根，不读 `scopeContext`**：`scopeContext` 与 `htmlPath` 不一致的行（历史/损坏数据）不能让一个挂载替另一个作答。
- **重试是"刷新 + 重新 probe"的完整单元**：不缓存、不重读上一次返回的 `File`（那个对象正是指向 rootfs 占位目录的那一个）。
- **`stale` 只在"绑定集合答不了这个问题"时为真**：既是竞态判据，也避免每次真删除都白跑一次 FUSE readdir。

### 2.2 调用点（`WebAppPathResolver.kt:57-68`）

```kotlin
private fun resolveMount(context: Context, htmlPath: String): File? {
    val hostPath = resolveMountWithRetry(
        htmlPath = htmlPath,
        mountKnown = { root -> PRootKernel.bindMounts[root] != null },
        probe = { path -> PRootKernel.resolveHostPath(path)?.absolutePath },
        usable = { path -> File(path).let { it.exists() && it.isFile } },
        refresh = { PRootKernel.applyMountedFoldersSnapshot(context) },
    )
    return hostPath?.let { File(it) }
}
```

### 2.3 明确没做的事（阶梯的"否决项"）

| 否决 | 理由 |
|---|---|
| 把 `MinisApp.onCreate:649` 的异步改同步 | 那是 `[dv20-A4-A06]` 的防首帧阻塞设计，且 `readonly_mount_write_guard.py` 的主线程 IO 禁令会当场判红 |
| 给 `resolveHostPath` 加全局锁 / 让 resolve 阻塞等快照 | 把一次读变成一次跨线程同步，WebApp 打开会等整个 SAF 快照；且解不了"快照还没开始跑" |
| 无限重试 / 轮询直至出现 | 一个真删除会变成无限轮询；`renderSourceMissing` 就永远不可达 |
| 改 `SCOPE_SHARED` | `/var/minis/{memory,skills,shared}` 的全局绑定由 `registerGlobalBindMounts` 在 `onCreate` 里**同步**注册，不存在这个窗口（已在代码里写明理由） |
| 改 `SCOPE_SESSION_ATTACHMENT` | 它的失败面是 canonical 前缀守卫（`safeResolveWithin`），与挂载无关 |

---

## 3. 改动清单

| 文件 | 类型 | 变化 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/webapp/MountResolvePolicy.kt` | 新增（生产） | +103 |
| `src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppPathResolver.kt` | 修改（生产） | +38/−? （`SCOPE_MOUNT` 改道 + `resolveMount` + KDoc） |
| `src/android/app/src/test/java/com/rikkaminis/app/webapp/MountResolvePolicyTest.kt` | 新增（测试） | +218（8 个行为用例 + 3 个接线用例，两个类） |
| `scripts/scan/webapp_asset_url_cleanup_check.py` | 修改（门） | +328/−25（docstring + 5 组夹具 + 3 判据 + `evaluate_tree` 拆分 + 6 个 selftest 臂） |

`git show --stat`：**4 文件 +662/−25**。冻结面（`scripts/scan/scan.sh`、`scripts/scan/test_scan.py`）**零触碰**；门总数仍 109、夹具仍 336（扩既有门，0 新建门）。

---

## 4. 门与判据

全部挂在既有门 `webapp_asset_url_cleanup_check.py`（它本来就扫 `WebAppActivity.kt`；本支顺带把 `WebAppPathResolver.kt` 纳入该门的文件覆盖，属同一脚本，未新建）。

| 判据 | 派生轴（不锚拼写） |
|---|---|
| **arm10** `check_mount_arm_snapshot_retry` | 从 `resolve()` 函数体里取 `SCOPE_MOUNT ->` 那一臂的**被调函数**作种子，按**本文件内的调用图**求传递闭包，要求闭包里同时出现 `resolveHostPath(`（重试要重新解析）与 `applyMountedFoldersSnapshot(`（重试要先刷新快照）。⇒ 刷新写在 `resolve()` 永远不调的函数里**不算**（这正是"文件级 grep 会判绿"的形状）；不写死函数名、不写死行号。 |
| **arm11** `check_activity_failure_guard` | 从 `WebAppPathResolver.resolve(` 调用点向前取最近的 `val <name> =` 作被解析值，要求 `if (<name> == null)` 出现在调用点之后，且 `renderSourceMissing(` 落在**该 if 的块内**、调用点与守卫之间不得出现 `renderSourceMissing(`。⇒ 堵住"未重试就早退"与"守卫挪到别的值上"。 |
| **arm12** `check_activity_resolve_offmain` | `resolve(` 调用点必须落在某个 `withContext(Dispatchers.IO) { … }` 块内；Activity 里任何 `applyMountedFoldersSnapshot(` 同样必须在这样的块内。⇒ 主线程 IO 判据的既有形状。 |

**带牙臂（selftest，全部断言"只红恰好其判据"）**：

| 臂 | 夹具 | 期望 |
|---|---|---|
| `e8-compliant` | 合规 resolver + 合规 Activity | 13 臂全绿 |
| `e8-no-retry pre-fix resolver` | `SCOPE_MOUNT -> PRootKernel.resolveHostPath(...)`（基线原形） | 仅 `arm10` |
| `e8-orphan-refresh` | 刷新函数存在但 `resolve()` 不可达（文件级 grep 会判绿） | 仅 `arm10` |
| `e8-unguarded-missing` | 去掉 `if (file == null)`，直接 `renderSourceMissing` + return | 仅 `arm11` |
| `e8-main-thread-resolve` | `val file = WebAppPathResolver.resolve(...)`（无 `withContext`） | 仅 `arm12` |
| `e8-main-thread-snapshot` | Activity 里直接 `applyMountedFoldersSnapshot(this)` | 仅 `arm12` |

**PRE 臂**（`probe/base/` = `git show f7aba218:` 抽出的 5 个文件，其余同名文件用当前树）：门输出 `FAIL arms=arm10`，rc=1 —— **恰好只红新判据**，arm1..arm9、arm11、arm12 在基线树上仍绿，证明新判据指向的就是本次修复面、没有借机把旧面弄红。

---

## 5. 验证四层

### L1 门层

| 项 | 结果 |
|---|---|
| 本门真树 | `PASS all 13 arms`，rc=0 |
| 本门 `--selftest` | `WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS`（37 行 selftest 输出，6 条 `e8-*` 全 ok） |
| PRE 臂（基线树） | `FAIL arms=arm10`，rc=1 |
| 全量门 | `python3 scripts/scan/scan.sh .` → **`Summary: 109 passed, 0 failed`**，rc=0（`probe/scan3.log`） |
| 夹具 | `python3 scripts/scan/test_scan.py .` → **`✅ ALL 336 CASES PASS`**，rc=0；本门两行在册：`webapp_asset_url_cleanup_check selftest has teeth (WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS)` + `real tree green` |

> PRoot 现场排污：前两次全量跑撞上 `python3: Function not implemented` 风暴（`Summary: 42 passed, 67 failed`，全为假红）与 180 s 无输出中断（rc=125），静置 ~60 s 后第三次前台跑 `rc=0 / 109 passed`。这与代码无关，日志 `probe/scan.log`、`probe/scan2.log`、`probe/scan3.log` 三段可对照。

### L2 邻门层

按概念（不是任务书点名的文件名）`grep -rln 'WebAppActivity\|WebAppPathResolver\|applyMountedFoldersSnapshot\|MountedFolders' scripts/scan/` 命中 4 道真实邻门，逐个 rc=0：

| 门 | rc | 与本次改动的关系 |
|---|---|---|
| `readonly_mount_write_guard.py` | 0 | 主线程 IO / 只读挂载写守卫；本次新增的 `applyMountedFoldersSnapshot` 调用在 IO 上下文内 |
| `claim_release_guard.py` | 0 | 引用 `PRootKernel` 的 claim/release 生命周期 |
| `share_dir_prune_check.py` | 0 | 引用 `applyMountedFoldersSnapshot` 的目录清理面 |
| `offload_host_path_check.py` | 0 | 宿主路径判据（同批 F 支另改，本支不触碰） |

（另有 `webapp_asset_url_cleanup_check.py` 本身 = 本门。）

### L3 装置层（真 kotlinc + JUnitCore）

`probe/l3/arm.sh <arm> <main-src> <cwd>`：单次 kotlinc 调用同时编 `MountResolvePolicy.kt` 与测试（`internal` 是模块级可见性，分行编译会失败——这也是 Gradle test source set 靠 friend-path 才能访问 `internal` 的同一件事），classpath 显式带 `junit-4.13.2 + hamcrest-core-1.3 + kotlin-stdlib`。

| 臂 | 性质 | 结果 |
|---|---|---|
| **post** | 真实现 | `OK (11 tests)`（8 个行为用例 + 3 个接线用例） |
| **pre** | 基线决策（`f7aba218` 的单次解析，同签名） | `Tests run: 11, Failures: 3`：`cold start refreshes…` / `the retry is bounded…` / `a throwing snapshot…` |
| **pre-wiring** | 同上 + cwd 指向基线源码树 | 4 红：上述 3 条 + `the mount arm resolves through the snapshot-retrying policy`（接线网在基线树上确实红） |
| **m1** | 重试**不重跑快照**（删掉 `refresh()`） | 3 红：`cold start` / `bounded` / `throwing` |
| **m2** | 刷新后**不清 `stale`** | 2 红：`bounded` / `throwing` |
| **m3** | **接受任何 probe 结果**（删 `usable` 判定 = 占位目录也算命中） | 3 红：`cold start` / `registered mount…` / `bounded` |

每条变异只施加在 `probe/l3/mut/<m>/MountResolvePolicy.kt` 的**副本**上，生产树全程零改动（跑前跑后 `sha256sum` 一致：`MountResolvePolicy.kt 7d09c1d4…`、`WebAppPathResolver.kt 982196f7…`）。

> **降级说明（COMMON §5 允许项）**：`WebAppPathResolver.kt` / `WebAppActivity.kt` 依赖 `android.content.Context`、`File`、`PRootKernel`，沙箱无 Android SDK 编不了 ⇒ 这两处的正确性由「门 + 真树 PRE 变异 + 接线用例（读源码断言）」补位，真正的编译保证来自 CI 的 `compileReleaseKotlin`（L4）。决策逻辑本身已抽成 Android 无关的纯函数，是被真 JVM 跑过的。

### L4 CI 层

见 §7 身份链。

---

## 6. 残留与负决策

| 编号 | 项 | 判级 | 触发条件（何时复活） |
|---|---|---|---|
| R1 | **真机欠账**：冷启动从桌面快捷方式打开 `mount` 范围 WebApp，不再出现 "WebApp source missing"（尤其 `SAF` 远端 provider 挂载） | P1 已验证设计，行为待真机 | 下次真机窗口；若仍死屏，说明竞态之外还有第二个失败面（先看 `applyMountedFoldersSnapshot: N mount(s) dropped` 告警） |
| R2 | `MOUNT_RESOLVE_MAX_ATTEMPTS = 2` 是硬编码 | P3 刻意 | 若出现"二次刷新才成功"的现场日志（`applyMountedFoldersSnapshot` 第一次 entries=0、第二次 >0），再谈是否加一次 |
| R3 | 竞态窗口在 `scopeContext` 与 `htmlPath` 不一致的历史行上按 `htmlPath` 判 | P3 刻意 | 若发现真实历史行两者不一致且行为异常 |
| R4 | `usable()` 用 `exists() && isFile`，符号链接穿过挂载根的情况未单独处理 | P3 既有语义 | `resolveHostPath` 的 `safeResolveWithin` 守卫变更时一并复查 |
| R5 | 门 arm10 依赖「`SCOPE_MOUNT` 那一臂调用本文件内的函数」这一形态；若将来把决策整段抽到别的文件，arm10 会 fail-closed 变红 | P3 刻意（fail-closed） | 真把决策移到新文件时，改动该门把新文件纳入扫描 |
| R6 | 门 arm9（既有）`decodeFileBounded` 与本次无关 | — | 不适用 |

**负决策（明确不做）**：把 `MinisApp.onCreate` 的异步快照改同步（见 §2.3）；给 `resolveHostPath` 加锁；无限重试。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（子提交 == 基线，开工前 `git log --oneline -1` 已核） |
| 生产/测试/门提交 | `8e9599b7` |
| 报告提交 | 见分支 HEAD（本文件，`fix-reports/` 不参与构建） |
| 分支 | `fix/dv26-e8-webapp-mount-coldstart` |
| CI run | **`39946138565`**（run #? of job `120389158628`，attempt **1**，`event=workflow_dispatch`，`conclusion=success`） |
| CI head_sha | `8e9599b7dbaba33ef07a3fd99ad1c228c4dcafa0` —— 与本地 HEAD、`git ls-remote` **三方逐字符一致** |

**L4 判读（`probe/ci/judge.py`，日志 1,432,073 B 落盘 `probe/ci/job.log`）**

| 判据 | 结果 |
|---|---|
| 26 个 step 的非 success 项 | **仅 `[23] Publish to Releases = skipped`**（main 门控） |
| 门汇总 | `Summary: 109 passed, 0 failed` **恰一条**，其余 7 条 Summary 全为 `N passed, 0 failed` |
| 门头连续性 | `[n/109]` 出现 109 次，`1..109` 连续且门名互异（unique=109） |
| 夹具 | `✅ ALL 336 CASES PASS` 恰一条 |
| 本门 | `webapp_asset_url_cleanup_check selftest has teeth (WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS)` + `real tree green` 两行在册 |
| 编译 | `e: ` 0 行、`Z e: ` 0 —— **无编译错** |
| 游离 FAILED | 0 行（不含 PASSED 用例名子串） |
| 新用例 | `MountResolvePolicyTest` **8/8 PASSED**、`WebAppMountColdstartWiringTest` **3/3 PASSED**（逐条在册） |

**停止节点 = 分支 CI 绿即停，不合并 main。**

---

## 8. 自证成本与不确定项

- **自伤 1**：`mountRootOf("/var/minis/mounts/index.html")` 我最初断言应为 null —— JVM 跑出来红，原因是**断言写错了**（`index.html` 是合法的挂载名，真实语义应返回该根）。改为 `/var/minis/mounts/`（空名）。这是本条唯一的"真失败"，且是测试的错误而非代码的错误。
- **自伤 2**：L3 首次运行全部 `initializationError` / `NoClassDefFoundError: kotlin/jvm/functions/Function1` —— classpath 漏了 `kotlin-stdlib.jar`。补上即绿。
- **装置成本**：L1 全量门跑 3 次（前两次 PRoot ENOSYS 风暴假红 + 一次 rc=125 中断）；L3 六臂各一次编译（~40 s/臂）。
- **不确定项**：门 arm12 的后半（Activity 内不得有主线程 `applyMountedFoldersSnapshot`）在当前树上是**空真**（Activity 里本来就没有该调用）——它防的是回归，不是现状；这一点已在该臂的夹具（`e8-main-thread-snapshot`）里证明它有牙。
