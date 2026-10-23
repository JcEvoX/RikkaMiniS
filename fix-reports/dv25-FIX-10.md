# FIX-10 · deep-audit-1045（v24 波）· B-06（P1）· MCP 覆盖播种的回填竞态

- **分支**：`fix/dv25-b6-mcps-merge-guard`
- **基线**：main `1d44ec905b8f5d21a215694e9a13910c74e1f645`（门 109 / 夹具 336）
- **停止节点**：分支 CI 绿（**未合并 main**）
- **门（独占）**：`scripts/scan/session_sheet_id_binding_check.py`（扩既有判据，**0 新建门**）

---

## §1 缺陷与根因

### 1.1 症状原话（报告 B-06）

会话 MCP 面板打开后的播种（把每个 server 的「本会话是否启用」从库读进本地 map）在读到用户刚点的开关时会把它**覆盖回旧值**——开关肉眼弹回，用户的选择丢失。

### 1.2 真树 Before（基线 `1d44ec90`，`SessionMcpsSheet.kt`）

```kotlin
:75    val overrides = remember(sessionId) {            // 会话级本地 map（Switch 直接读它）
:78    LaunchedEffect(servers, sessionId) {
:83        val missing = servers.map { it.id }.filter { it !in overrides }   // ← 快照（Main）
:85            missing.associateWith { mcpRepository.isEnabledForSession(it, sessionId) }  // ← await（IO）
:87        for (id in missing) overrides[id] = seeds[id] ?: false            // ← 无守卫回写
:91        overrides.keys.retainAll(live)
```

### 1.3 根因（一句话）

`missing` 是**await 之前的快照**，`overrides` 是**活的 map**；第 87 行对整个快照无条件回写，于是「await 期间发生的用户 toggle」被它自己的**读前旧值**覆盖。

### 1.4 证据（≥1 条，实际三条）

1. **形态证据**：同文件第 88 行的 `Switch(checked = overrides[server.id] ?: …)` 证明 `overrides` 就是屏上开关的来源 —— 被覆盖 = 开关弹回，不是「只是缓存不一致」。
2. **孪生对照**：`SessionSkillsSheet.kt:270-282` 的 `mergeSessionOverrides` 第一行就是守卫（`:276 if (id !in overrides)`），其 KDoc 明写 *“Contract — deliberately identical to `SessionMcpsSheet`'s seeding effect”* —— 两个文件在注释里宣称同一契约，**只有一处在代码里实现了它**（这正是缺陷的成因：契约被抄了两份）。
3. **机制对称性**：同一 sheet 的行内 toggle（`:120-131`）先改本地 map 再异步写库；写库落地前 map 就是唯一真相 —— 快照回写正好打在这段窗口上。

### 1.5 影响面（为什么是 P1）

用户可见 + 用户选择丢失，但**可自愈**（再点一次），且只发生在「effect 在飞」窗口（一次 opening 一帧到数百 ms，服务器多时 IO 读是 per-id SQLite 查询，窗口随 server 数线性变长）。不写库损坏、不丢持久数据。

---

## §2 修法

### 2.1 停在第 2 级（代码库里已有）

> **停在第 2 级，因为 `SessionSkillsSheet.kt` 里已有 `internal fun mergeSessionOverrides(liveIds, overrides, lookup)`（同包、同模块、internal 可见），它的第一行就是本缺陷需要的守卫，并且顺带处理了 `retainAll`。**

生产改动只有一处：把第 83-91 行的手写循环换成「快照 → await → 调既有 helper」：

```kotlin
val liveIds = servers.map { it.id }
val seeds = withContext(Dispatchers.IO) {
    liveIds.filter { it !in overrides }
        .associateWith { mcpRepository.isEnabledForSession(it, sessionId) }
}
mergeSessionOverrides(
    liveIds = liveIds,
    overrides = overrides,
    lookup = { seeds[it] ?: false },
)
```

`mergeSessionOverrides` 在 Main 上**重新判定**（`if (id !in overrides)`），所以 await 期间的 toggle 不在「仍然缺失」集合里，不会被写；`retainAll` 移到 helper 内（语义不变：消失的 server 的 stale toggle 仍被丢弃）。

### 2.2 硬约束逐条核对

| 约束 | 处置 |
|---|---|
| 保住 `remember(sessionId)` 键 | 一行未动（`:76`） |
| 保住 `LaunchedEffect(servers, sessionId)` 触发条件 | 一行未动（`:78`） |
| 保住 `__new__` 种子语义 | 会话 id 变化仍换新 map（键在 `remember` 上），`?: false` 的 totality 回退保留（helper 的 `lookup` 仍是 lambda） |
| 不得把 seeding 移出 Main | helper 在 Main 上跑；只有 `isEnabledForSession` 的读仍在 `withContext(Dispatchers.IO)` |
| 不改 `SessionSkillsSheet.kt` | 零改动（只**调用**其函数，不编辑文件） |
| 终止路径 | 无新增挂起点；`withContext` 被取消时 `mergeSessionOverrides` 不会执行（取消 = 沿用 Compose 的 effect 取消语义，未改动） |

### 2.3 被否决的方案

- **任务书方向一：新建 `mergeMcpOverrides` 纯函数**（与 skills 侧「同款」）。**否决**：本次缺陷的成因就是「同一契约存在两份实现、修一份漏一份」；再加第三份副本会把漂移面扩大（`SessionSkillOverrideMergeTest` 已固化 skills 侧行为，副本会各自演化）。复用既有函数等于把「两份契约」收敛成一份，属阶梯第 2 级。
- **任务书方向二：最小内联守卫**（`for (id in missing) if (id !in overrides) overrides[id] = seeds[id] ?: false`）。**否决**：能修行为，但仍然是第二份实现；且 `retainAll` 与 `?: false` 的 totality 论证仍需各写一遍。
- **改用批量读 `MCPRepository.sessionEnablementMap`**（`dv22-B4` 的批量孪生，一次 `IN (…)` 查询替代 per-id）：**半径外**，本缺陷不是性能问题；已登记 R5（触发条件见下）。

---

## §3 改动清单

| 文件 | 类型 | 增/删 | 区间 |
|---|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/SessionMcpsSheet.kt` | 生产 | **+24 / −7** | `@@ -80,15 +80,32 @@`（`LaunchedEffect` 体；文件 180 行不变域外） |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/chat/SessionMcpOverrideMergeTest.kt` | 测试（新增） | **+343 / −0** | 全文（8 用例 + 2 个形状助手） |
| `scripts/scan/session_sheet_id_binding_check.py` | 门（扩既有） | **+380 / −1** | `@@ -39,6 +39,31 @@`（docstring 判据）· `@@ -197,6 +222,155 @@`（`check_seeding_guard` 机制）· `@@ -350..368 @@`（挂到两个 sheet）· `@@ -576,6 +757,151 @@`（8 个夹具）· `@@ -679,6 +1005,59 @@`（8 + 4 臂）· `@@ -812,7 +1191,7 @@`（OK 文案） |

合计 **3 文件 +747 / −8**。**门数 109 → 109、夹具 336 → 336**（零新建门；`scan.sh` / `test_scan.py` 冻结面零触碰）。

提交（3 个，分行）：

```
bd574457 fix(dv25-b6): seed the MCP override map through the shared merge helper
0bbda65e test(dv25-b6): pin the MCP seeding contract — behaviour + wiring
ea800a0a gate(dv25-b6): judge the seeding write, not the file, on both sheets
```

---

## §4 门判据与带牙证明

### 4.1 判据（派生式，不写死变量名）

`check_seeding_guard(code, label, findings)`：

1. **派生 map**：`([A-Za-z_]\w*)\s*=\s*remember\s*\(\s*sessionId\s*\)` —— 不认 `overrides` 这个名字。
2. **派生作用域**：每个 `LaunchedEffect(<键表含 sessionId>) { … }` 的**函数体**（brace 匹配）。**只在 effect 体内判** —— 行内 toggle（`onCheckedChange` 里的 `overrides[server.id] = enabled`）本就无守卫，不该被判。
3. 对体内每个 `<map>[…] =` 写入：
   - **受守卫** = 写入**紧邻的** `if (…)` 条件里重新判定「该 id 是否仍不在这个 map 中」，三种等价拼写都认：`x !in map` / `!map.containsKey(…)` / `map[…] == null`（锚决定点不锚拼写）；
   - 或该 effect 体调用 `merge[A-Za-z0-9_]*Overrides?(` **家族 helper**，且这个调用的实参里**出现该 map**；
   - 两者都不满足 → 红（报 `文件:行`）。
4. **fail closed**：体内既无写入、也无「收到该 map 的家族 helper 调用」→ 红（种子语句被删掉/搬走，而不是被修好）。
5. `if (id in missing)` 这类**守卫在快照上**的写法不认 —— 那正是缺陷换了个说法。

### 4.2 为什么必须「按写入点」判（本支最关键一条）

基线文件**含有** `it !in overrides`（第 83 行的 `missing` 过滤里就是这么写的）。任何**文件级** grep（甚至「effect 体内出现过 `!in overrides`」）都会把基线判绿 —— 这正是该门此前的盲区，也是任务书 §4 要求「基线形态 → 红」的原因。

### 4.3 带牙臂（selftest 16 → **28** 条 ok）

| 臂 | 输入 | 期望 | 实测 |
|---|---|---|---|
| 基线回写形态 `FIX_MCPS_NOGUARD` | 快照 + 无守卫 for 回写（真树基线逐字） | 1 finding | ✅ `mcps baseline: snapshot-unguarded seed loop: 1 finding(s)` |
| 已发布形态 `FIX_MCPS_MERGE` | 调 `mergeSessionOverrides(overrides = overrides, …)` | clean | ✅ |
| 直守卫（无花括号）`FIX_MCPS_BRACELESS` | `if (id !in overrides) map[id] = …` | clean | ✅ |
| 拼写二 `FIX_MCPS_NULLREAD` | `if (overrides[id] == null) …` | clean | ✅ |
| 拼写三 `FIX_MCPS_CONTAINSKEY` | `if (!overrides.containsKey(id)) …` | clean | ✅ |
| 守卫锚错 map `FIX_MCPS_WRONG_MAP` | `if (id !in seeds) overrides[id] = …` | 1 finding | ✅ |
| helper 没收到 map `FIX_MCPS_HELPER_BLIND` | `mergeSessionOverrides(liveIds = …, lookup = …)`（无 map 实参）+ 无写入 | 1 finding（fail closed） | ✅ |
| 孪生同款缺陷 `FIX_SKILLS_NOGUARD` | skills 侧无守卫回写 | 1 finding（**同一判据判两个文件**） | ✅ |
| 精确性 ×4 | 上述四个红臂 | 各**恰好 1 条**，且 finding 文本含 `SessionMcpsSheet.kt` / `unguarded seeding write` / `in-flight toggle` / `merge*Overrides` / `SessionSkillsSheet.kt` | ✅ |

**既有 16 条臂全绿未回归**（`compliant tree` / `dirty chatscreen` 10 / `skills rebuild` 3 / `skills keyless` 2 / `mcps keyless` 3 / edit-sheet 家族 2·1·5 / 各 precision 计数）。

### 4.4 真树与基线树的对照（L1 的 PRE 取证）

```
POST  (工作树)         python3 scripts/scan/session_sheet_id_binding_check.py .        → rc=0  OK
PRE   (probe/pre 树，SessionMcpsSheet.kt 取 git show 1d44ec90:…)                       → rc=1
      SessionMcpsSheet.kt:87: unguarded seeding write `overrides[…] =` inside the LaunchedEffect …
```

PRE 的 finding 精确落在基线第 87 行 —— 与任务书给的 `:87 无守卫` 逐字对应。

---

## §5 验证四层结果

### L1 门（本门 + 全量）

- 本门真树：`session_sheet_id_binding_check: OK (… both sheets id-keyed with incremental seeding through membership-guarded writes …)`，rc=0
- `--selftest`：**28 条 ok / 0 FAIL / `SESSION_SHEET_ID_BINDING_SELFTEST=PASS`**（arm 数 16 → 28）
- 全量 `sh scripts/scan/scan.sh`：**`Summary: 109 passed, 0 failed`**，`━━━ [N/109]` 头部 **109** 条、无 `Function not implemented`
- `python3 scripts/scan/test_scan.py`：**`✅ ALL 336 CASES PASS`**
- 首跑自伤（已闭环）：第一次全量扫描撞 PRoot `Function not implemented` 风暴（8 个 gate 连 `python3` 都起不来 → 假红 rc=1）；`test_scan` 首跑 `OSError: [Errno 38]` 在 `subprocess.wait`。按沙箱纪律「错开窗口重跑」，第二次两者均全绿（日志 `probe/full-scan.log` → `probe/full-scan2.log`、`probe/test_scan.log` → `probe/test_scan2.log`）。

### L2 邻门（按概念反查，非照抄任务书名）

`grep -rln "SessionMcpsSheet\|SessionSkillsSheet\|mergeSessionOverrides" scripts/scan/*.py` → 除本门 2 个；再按「会话级开关 / 同族 UI 交互 / off-main IO / remember 键」补 3 个：

| 门 | rc |
|---|---|
| `chat_session_id_binding_check.py`（同会话 id 绑定概念） | 0 |
| `session_provider_enabled_check.py`（同族「会话级 enabled」开关） | 0 |
| `chat_ui_interaction_check.py`（同族 chat UI 交互） | 0 |
| `skill_import_offmain_check.py`（off-main IO 纪律） | 0 |
| `provider_remember_key_check.py`（remember 键纪律） | 0 |

**5/5 rc=0**。

### L3 真跑（`kotlinc` 2.0.21 + JUnit 4.13.2）

装置 `probe/l3/`（`extract.py` 只写字节、`arm.sh` 独占子进程 —— 遵守「分层驱动」纪律）。五个臂，每个臂自带 `Merge.kt`：

- POST / M1 / M2 / M3 的 `mergeSessionOverrides` = **从 `SessionSkillsSheet.kt` 逐字节切出的真函数**（切片脚本硬断言含 `if (id !in overrides)` / `overrides.keys.retainAll(live)` / `lookup(id)`，切片不完整即 exit 1）；
- PRE = 基线播种语义（`missing` 在 await 前冻结 + 无条件回写）；
- 每个臂再带一段 `runSeedEffect(...)`（sheet 的 Compose effect 的纯函数转写 + `duringIoWindow` 钩子模拟 await 窗口 —— Compose 不能在 JVM 上跑，这是唯一无法直编的一层，逐行对照 `:78-95`）。

行为用例 A1..A6（窗口内 toggle ON / 窗口内 toggle OFF / 未触碰项照常播种 / 不为已翻转 id 二次查库 / 消失 id 丢弃后可重播种 / 草稿晋升重新播种）：

| 臂 | 结果 | 红的用例 |
|---|---|---|
| **POST**（真函数） | **OK (6 tests)** | — |
| **PRE**（基线语义） | 3 失败 | A1 窗口内 toggle ON · A2 窗口内 toggle OFF · A4 二次查库 —— **恰好「在飞 toggle」家族** |
| M1（守卫中和 `if (true)`） | 4 失败 | A1 · A2 · A4 · A5 |
| M2（删 `retainAll`） | 1 失败 | A5 |
| M3（守卫取反 `if (id in overrides)`） | 6 失败 | 全部 |

**仓库内真实测试文件的独立复跑**（同一 `SessionMcpOverrideMergeTest.kt`，classpath 换成对应臂）：

| classpath | 结果 |
|---|---|
| 真函数切片（= 已发布形态） | **OK (8 tests)** |
| 未守卫的 merge 实现（PRE） | **3 失败**：`in flight toggle survives…` / `lookup is not consulted…` / `vanished servers are dropped…`；**两条 source-shape 用例仍绿**（它们读的是真文件，真文件已修） |

### L4 CI

| 项 | 值 |
|---|---|
| run | **#2543 `39916936078`**（event=`workflow_dispatch`，ref=`fix/dv25-b6-mcps-merge-guard`） |
| job | **`120292423692`** attempt 1 |
| head_sha | `ea800a0a31412a79e94552b5a64a5953dcf3cc23` = 本地 `git rev-parse HEAD` = `git ls-remote origin <branch>`（**三方逐字符一致**） |
| conclusion | **success**；`non-success steps = [(23, 'Publish to Releases', 'skipped')]` —— **唯一 skipped = main 门控**，其余 25 步全 success |
| 门判读 | 8 条 `Summary:` 中**恰一条 `109 passed, 0 failed`**，无任何非零 failed 行 |
| 门头 | `━━━ [N/109]` **109 条、1..109 连续且互异** |
| 夹具 | `✅ ALL 336 CASES PASS` |
| 本门在册 | `session_sheet_id_binding_check: OK (… membership-guarded writes …)` + `✅ … selftest has teeth (SESSION_SHEET_ID_BINDING_SELFTEST=PASS)` + `✅ … real tree green` |
| 新增用例 | `SessionMcpOverrideMergeTest` **8/8 PASSED**（逐条在册，含 `source shape assertions have teeth`） |
| 编译 | `Z e:` **0**（0 error / 0 warning 形态）；游离 `FAILED` **0** |
| 日志 | `probe/ci/job.log` **1,412,221 B** |

**三源取二**：①CI 绿（L4）②沙箱真跑（L3 五臂 + 真测试文件 8 用例）③本地全量门 + 336 夹具（L1）—— 三条独立成立。

---

## §6 任务书失真（登记，不阻塞）

- **D1｜`check_skills_sheet` 并没有 merge 判据**：任务书 §2 写「门盲区：`check_mcps_sheet`（`:356-371`）只查 `remember(sessionId)` 与 `LaunchedEffect(servers, sessionId)`，无 merge 判据（`check_skills_sheet` 有）」。真树核对（`git show 1d44ec90:scripts/scan/session_sheet_id_binding_check.py`）：`check_skills_sheet`（`:320-353`）同样只有 `remember(sessionId)` / 重建形态 / keyless / `LaunchedEffect(skills, sessionId)` 四条，**没有任何 merge-write 判据**；它的 docstring 只是**宣称**「Same incremental-merge criterion as check_mcps_sheet」。⇒ **两个 sheet 此前都是盲的**，不是「一侧有一侧无」。本支按「一份契约一处判定」把新判据同时挂到两侧（`FIX_SKILLS_NOGUARD` 臂取证）。
  - 未按任务书只补 mcps 侧的理由：只补一侧会让「下次有人在 skills 侧写回无守卫循环」重新静默；且这与缺陷成因（契约两份实现）是同一个错误面。
- **D2｜行号全对**：`SessionMcpsSheet.kt:78-92`（实测 78-92）· `:87 无守卫`（实测 87）· `SessionSkillsSheet.kt:276 if (id !in overrides)`（实测 276）· 门 `:356-371`（实测 356-371）。无行号失真。

---

## §7 残留（R1..Rn，每条带触发条件）

- **R1｜真机行为级（P1 验证面）**：会话内打开 MCP 面板，在面板刚出现时快速连点某 server 的开关（尤其 10+ server 时）→ 开关**不弹回**，且退出重进后保持为用户选的值。**触发条件** = 下次真机窗口。**触发后方案** = 进 `§1045-VERIFY` 一次开齐。**搁置理由** = 沙箱能证决策层（真函数 + 真测试 + 门），证不了 App 进程内的 Compose 时序与真实 SQLite 延迟。
- **R2｜`isEnabledForSession` 仍是 per-id 读（P2 性能，半径外）**：`MCPRepository.sessionEnablementMap`（`dv22-B4` 的批量孪生）已存在，本支未切换 —— 本缺陷不是性能问题，切批量会同时改动 `seeds` 的构造与 `?: false` 的 totality 论证。**触发条件** = 有人报「MCP server 多于 ~30 个时打开面板卡顿」或 skills 侧先切批量。**触发后方案** = `seeds = withContext(IO) { mcpRepository.sessionEnablementMap(sessionId, missing) }`，`lookup` 保持 `seeds[it] ?: false`。**搁置理由** = 既定半径（任务书只要求接守卫）。
- **R3｜skills 侧的 KDoc 仍写着「identical to SessionMcpsSheet's seeding effect」（P3 文档）**：现在字面为真（两处都调 `mergeSessionOverrides`），但**表述仍像「两份实现」**。**触发条件** = 下次动 `SessionSkillsSheet.kt`（本支禁区内）或有人按 KDoc 又抄一份。**触发后方案** = 改成「both sheets delegate to `mergeSessionOverrides`」。**搁置理由** = 禁区文件只读；且改注释需要另一个支的验证面。
- **R4｜门只判「写入点附近」的守卫（P3 形态）**：守卫写在 `map[id] ?: run { … }`、`takeIf` 链、或先 `val pending = liveIds - overrides.keys` 再在另一函数里写，本门都会判红（方向安全：红而非静默绿）。**触发条件** = 有人重构成上述形态且 CI 红。**触发后方案** = 把 `pending` 这类「派生集合」纳入派生（认「写入的 key 集合 = 派生自 `!in map` 的集合」）。**搁置理由** = 现有代码两种形态（直守卫 / helper）都覆盖，为不存在的形态加机制违反装置比例纪律。
- **R5｜`?: false` 的 reachability 只被注释论证（P3）**：`seeds` 覆盖 merge 会问的 id 集合，理论上 `?: false` 不可达；若将来 `liveIds` 与 `missing` 的来源分叉（例如一边过滤一边不过滤），fallback 会静默把「未知」当「禁用」。**触发条件** = 有人改 `liveIds` / `seeds` 的构造（尤其改成两处不同来源）。**触发后方案** = 让 helper 接受 `Map` 直投（`lookup = seeds::getValue`，缺失即抛）+ 测试补一拍。**搁置理由** = 当前构造下不可达，改签名会同时动 skills 侧（R3 同一禁区）。
- **R6｜JVM source-shape 用例依赖 `user.dir` 向上找仓根（P3 装置）**：CI 在 `src/android` 下跑 `./gradlew testReleaseUnitTest`，靠 `BrowserTabPoolDisposeScopeTest` 同款向上查找命中仓根；若将来 gradle 改 working dir 语义会 `FileNotFoundException`（**红**，方向安全）。**触发条件** = 构建脚本改 working dir。**触发后方案** = 用 `System.getProperty("rikka.repo.root")` 或 gradle test 的 `workingDir` 显式注入。**搁置理由** = 现有 6 个同类测试共用该约定，改动应一次性做。
- **R7｜门首版自伤（已修，规则级）**：`SEED_WRITE_TMPL` 首版没有 lookbehind `(?<![\w.])`，会把 `seeds[id] = ` 之类**别的 map** 的写入也算作「对该 map 的写入」（本支靠 `FIX_MCPS_WRONG_MAP` 臂当场暴露）。修法 = 写入模式带负向后视 + 守卫条件里必须出现**该** map 名。**触发条件** = 再写「按变量名派生写入点」的判据。**触发后方案** = 一律 use `(?<![\w.])<var>`（防 `smap` / `foo.map` 误命中）。**搁置理由** = 本支已修，留作跨批规则。
- **R8｜任务书 §5 指定的 `mergeMcpOverrides` 不存在（已由 §2.3 记录）**：任务书要求的「`mergeMcpOverrides` 双臂」按 §2.3 的否决理由改成「真函数 `mergeSessionOverrides` 双臂 + 接线双臂」，L3 逐臂取证见 §5。**触发条件** = 主会话裁定必须保留同名纯函数。**触发后方案** = 把 `mergeSessionOverrides` 改名为 `mergeOverrideSeeds`（两侧同时改，仍是一份实现）。**搁置理由** = 增加副本违反阶梯第 2 级。

---

## §8 身份链

| 环 | 值 |
|---|---|
| 基线 main | `1d44ec905b8f5d21a215694e9a13910c74e1f645`（= 门 109 / 夹具 336） |
| 本地 HEAD | `ea800a0a31412a79e94552b5a64a5953dcf3cc23` |
| 远端 `origin/fix/dv25-b6-mcps-merge-guard` | `ea800a0a31412a79e94552b5a64a5953dcf3cc23` |
| CI `run.head_sha` | `ea800a0a31412a79e94552b5a64a5953dcf3cc23`（三方逐字符一致） |
| CI | run **#2543 / `39916936078`** · job **`120292423692`** · attempt 1 · **success** |
| 提交 | `bd574457` 生产 · `0bbda65e` 测试 · `ea800a0a` 门 |
| 报告回填 | 本文件与 backlog 段作为第 4 个提交追加；`git diff --name-only ea800a0a HEAD -- . ':(exclude)fix-reports'` 为空 ⇒ **源码树等价**，CI 结论对当前树成立 |

**未合并 main**（含 `.kt` 验证面，起手权在主会话/用户）。
