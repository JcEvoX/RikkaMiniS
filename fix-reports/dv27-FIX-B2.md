# FIX-B2（deep-audit-1047 / B-04，P1）— 组选择/成员解析三处失步

- 分支：`fix/dv27-b2-group-selection-sync`
- 基线：main `88ef9b27`
- 停止节点：本支 CI 绿（**未合并 main**，起手权在用户）
- 任务书：`/var/minis/shared/work/deep-audit-1047/fix-tasks/FIX-B2.md`

---

## 1. 缺陷与真树取证（基线 88ef9b27）

三处失步，都在「哪个成员为本次会话服务」这个问题的拥有者身上：
`ui/chat/ChatModelRouting.kt`（选择动作）与 `ui/chat/ChatSessionLifecycle.kt`（解析动作）。

### 1.1 断层 ①：显示态先发布，可用性后判（形状 30）

`ChatModelRouting.kt` 基线 `selectGroup`：

```
:81   _selectedGroupId.value = groupId          ← 先写
:82   _selectedGroupName.value = ... ?: ""
:87   pendingEntryOverride = null
:88   val resolved = resolveProviderFromGroup(groupId)   ← 后判
:89   if (resolved) { … }
```

`selectGroupEntry`（`:110-135`）同构。当 `resolveProviderFromGroup` 返回 `false`
（组内只有 `SystemVoiceEntries` 虚拟哨兵 / 全组未填 key / provider 全禁用）时：
顶栏组名与 picker 高亮已经换成了新组，而 `currentProvider` / `currentModel` /
`_activeEntryId` / 数据库 `modelBinding` 全部停在旧模型上 ⇒ **显示态与执行态分裂**。

调用点：`ChatScreen.kt:5009,5066`（含 `PendingNonTextSelection` 弹窗的「仍然使用」）、
`:5024,5068`。

### 1.2 断层 ②：同一次调用两处读不同 id（形状 38/30）

`selectGroupEntry` 基线：

```
:124  if (!_isStreaming.value) pendingEntryOverride = entryId          ← 原始入参
:125  persistBinding("""{… "lastEntryId":"$entryId"}""")              ← 原始入参
:130  // resolveProviderFromGroup may fall back off a disabled member,
      // so `_activeEntryId` is the truth
:131  _activeEntryId.value?.let { providerRepository.lastUsedEntryId = it }   ← 另一个 id
```

注释自己写着「`_activeEntryId` is the truth」，但**持久化的 `lastEntryId` 用的却是原始
入参**。回退发生时（用户点的成员不可用 ⇒ 落到别的成员），会话绑定指向的是那个
**刚被跳过的成员** ⇒ 下一轮 `rotateForNewTurn` 与重开会话继续踩它。

### 1.3 断层 ③：三处漏判单源（形状 30）

- `selectEntry`（`:137-160`）：`entry` → `instance` → `apiKey` 三步直查，
  **未走** `decideSessionModelResolution`（`ChatSessionLifecycle.kt:1087`，含
  `!instance.isEnabled` 判定）⇒ 经 `HeadlessChatRunner.kt:931` / 弹窗
  `ChatScreen.kt:5069` 传入一个「有 key 但 provider 已禁用」的条目会
  **绕过禁用守卫直接激活**。
- `resolveProviderFromGroup`（`ChatSessionLifecycle.kt:1185`）与 `rotateForNewTurn`
  （`ChatModelRouting.kt:455`）：只用 `enabledMemberEntries(group)` 过滤
  `instance.isEnabled`，**未过滤无凭据成员**；`:1205`（resolve）/`:465`（rotate）
  在排头成员无 key 时直接 `return false` / `return` ⇒ **整组不可用**
  （而非跳过该成员、落到后面那个有 key 的成员）。

---

## 2. 修法（改动阶梯第 6 级：状态写入收敛到「已解析」之后 + 单源漏斗）

> **停在第 6 级**：仓库里**已有** `decideSessionModelResolution` 这个单源漏斗，
> 但 `selectEntry` 与两个组消费者**没走它** ⇒ 不是新增机制，而是把既有单源接上。

### 2.1 断层 ①：显示态移入 resolved 分支

`selectGroup` / `selectGroupEntry` 的 `_selectedGroupId` / `_selectedGroupName` /
`pendingEntryOverride = null` 全部移进 `if (resolved)` 块内。`resolved == false` 时
**保持原选择不变**（调用点签名返回 `void`，不动签名）。

**`groupRouter.clearHealth()` 刻意留在 resolution 之前**，并在代码里写明理由：
清健康是让被降级/熔断的成员重新可用的手段，必须发生在「让路由器挑成员」之前；
挪进成功分支会让一个全降级的组**再也无法通过重选恢复**。这是路由状态，不是本单
要修的显示态。

### 2.2 断层 ②：一个 id 走到底

新增 Android 无关纯函数文件 `ui/chat/GroupSelection.kt`：

```kotlin
data class GroupSelectPlan(val applyUi: Boolean, val effectiveEntryId: String?)

fun planGroupSelect(resolved: Boolean, resolvedId: String?, requestedId: String): GroupSelectPlan =
    when {
        !resolved            -> GroupSelectPlan(applyUi = false, effectiveEntryId = null)
        resolvedId != null   -> GroupSelectPlan(applyUi = true,  effectiveEntryId = resolvedId)
        else                 -> GroupSelectPlan(applyUi = true,  effectiveEntryId = requestedId)
    }
```

`selectGroupEntry` 里 `pendingEntryOverride` / `persistBinding(lastEntryId)` /
`lastUsedEntryId` 三处**统一用 `plan.effectiveEntryId`**（即解析后真正服务的成员）。
第三臂（`resolvedId == null`）兜回 requested id —— 解析没有发布任何 active entry 时，
调用方要的 id 就是即将激活的那个。

### 2.3 断层 ③：三处走同一个漏斗

- `selectEntry`：改走 `decideSessionModelResolution(entry, instance, apiKey)`；
  `Unusable` 时**不激活**并 `return`，并调用既有的 `logSessionModelUnusable`
  （该函数由 `private` 放宽为 `internal`，因为 `selectEntry` 在另一个文件）。
  provider 构造改用漏斗持有的 `decision.instance` / `decision.apiKey`。
- 新增 `internal fun ChatViewModel.usableGroupMembers(members: List<ModelEntry>)`：
  在 `enabledMemberEntries` 之后再过一遍漏斗（`is Use` 才留）。
  `resolveProviderFromGroup` 与 `rotateForNewTurn` 两个消费点都改用它 ⇒
  `:1205`/`:465` 的 `?: return false` 只在「全部成员都不可用」时命中。

**性能与语义核实**：凭据读取用的是 `providerRepository.loadApiKey(instance.id)`，
与既有两处同构先例一致 —— `buildFallbackProviders:401` 与
`ConfigBuiltins.firstUsableGroupMember:909` 都是在候选循环里按 `instance.id` 调它。
本单只是把这次读取**挪到选择之前**而不是之后。

**单测 fake 未对 `loadApiKey` 调用次数做严格断言**（任务书要求复核）：全仓
`src/test` 里对 `loadApiKey` 的唯一计数断言在
`ProviderCredentialLiveSourceTest:206`，它数的是 **`ProviderConnectionScreen.kt`**
里 `loadApiKey(` 的字面出现次数（恰好 1），与本单改的聊天层文件无关 ⇒ 不受影响。

### 2.4 必须保住的（逐条核过）

| 契约 | 结论 |
|---|---|
| 8 处 `ProviderFactory.create` 的 `providerModelFor(...)` 富化实参形态 | 未动；门仍报 8 个站点，ROT 计数不变 |
| `loadBalance` 语义：首条消息不轮转、显式挑成员不推进全局 `lastUsedEntryId` | 未动（`resolveProviderFromGroup` 里 `preferredEntryId == null && loadBalance` 的那行原样保留） |
| `restoreFromBinding` 的 `if (resolved) _selectedGroupId.value = groupId` | 未动（本来就是正确形态，门对它设了精度臂，见 §4.4） |
| `RoutingStrategy` 定义 | 未动 |
| 语言键 | 零新增（7+1 语言文件零改动） |

---

## 3. 改动清单（5 文件，+604/−19）

| 文件 | 性质 | 改动 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatModelRouting.kt` | 生产 | +60/−9（`selectGroup` / `selectGroupEntry` / `selectEntry` / `rotateForNewTurn`） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatSessionLifecycle.kt` | 生产 | +35/−2（`usableGroupMembers` 新增、`logSessionModelUnusable` 放宽、两个消费点改过滤） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/GroupSelection.kt` | 生产（新增） | +59（`GroupSelectPlan` / `planGroupSelect`） |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/chat/GroupSelectionPlanTest.kt` | 测试（新增） | +89（7 个用例） |
| `scripts/scan/session_provider_enabled_check.py` | 门 | +359（C-SEL-1/2/3 三条判据 + selftest 臂） |

**门数不变（111）、夹具数不变（340）** —— 本支扩既有门，**0 新建门**，
`scan.sh` / `test_scan.py` **零触碰**（本波新规：冻结面由主会话在并集阶段统一注册）。

---

## 4. 门与判据（扩 `session_provider_enabled_check.py`，0 新建门）

原有判据只覆盖 `ChatSessionLifecycle.kt` 的 4 个恢复站点 + `ProviderFactory.create`
第 3 实参富化。本次新增三条，**全部从树派生**：

### 4.1 C-SEL-1（状态写入必须有门）

`ChatModelRouting.kt` / `ChatSessionLifecycle.kt` 里写 `_selectedGroupId.value` /
`_selectedGroupName.value` 的每一处，必须位于「解析已给出答案」的分支内。

- **守门者从树派生**：`resolved` / `plan` / `decision` 这些名字**不是写死的** ——
  判据找出所有「右值调用了 `resolveProviderFromGroup(` / `planGroupSelect(` /
  `decideSessionModelResolution(`」的赋值，把那些名字当作合法守门者。
  改名不失明，把解析内联进 `if` 也合法（那时守门者就是调用本身）。
- **块判定用缩进而非括号配平**：这些文件带 `"""{"type":"group",…}"""` 字面量，
  它的花括号会污染深度计数；格式化的缩进是可靠的。
- **豁免解析器自身**：`resolveProviderFromGroup` 内部那句
  `_selectedGroupName.value = group.name` 是解析成功路径上的发布，
  要求它「被解析守卫」就是循环论证 ⇒ 显式豁免（`_is_resolver`）。

### 4.2 C-SEL-2（同一次调用同一 id）

凡函数体内同时写持久化的 `lastEntryId` 与读 `_activeEntryId` 的，持久化用的那个
id 表达式必须**追溯到** `_activeEntryId`（沿局部 `val` 赋值链最多追 3 跳）。
直接用原始入参 ⇒ 红。

### 4.3 C-SEL-3（单源漏斗）

- `selectEntry` 必须经 `decideSessionModelResolution`；
- `enabledMemberEntries(` 的每个消费点在交给 router 之前必须经漏斗过滤 ——
  合法的过滤器名字**从树派生**（签名含 `List<ModelEntry>` 且体内咨询漏斗的函数），
  所以重命名 helper 门仍绿、删掉 helper 则所有消费点变红。

### 4.4 带牙臂

| 臂 | 期望红 | 实测 |
|---|---|---|
| 合规夹具 | 无 | 0 违规 ✔ |
| PRE（基线 `88ef9b27` 的两个文件） | C-SEL-1 / C-SEL-2 / C-SEL-3 | 8 条违规，三条判据各中 ✔ |
| 真树变异 m1（把 `_selectedGroupId.value` 挪回函数首行） | 恰 C-SEL-1 | 恰 1 ✔ |
| 真树变异 m2（`persistBinding` 用原始 `entryId`） | 恰 C-SEL-2 | 恰 1 ✔ |
| 真树变异 m3（`selectEntry` 退回三步直查） | 恰 C-SEL-3 | 恰 1 ✔ |
| 真树变异 m4（rotation 跳过凭据过滤） | 恰 C-SEL-3 | 恰 1 ✔ |
| 精度臂 m5（解析器自身的发布） | 无 | 0 ✔ |

`m3` 的变异形态值得一提：把漏斗调用换成
`SessionModelResolution.Use(entry, instance!!, apiKey!!)` —— 即「假装漏斗说过可以」。
它只红 C-SEL-3，说明判据锚的是**调用了漏斗**这件事，而不是漏斗的返回值。

**PRE 臂是真对照**：基线文件用 `git show 88ef9b27:` 从 git 对象库取出，
不是脏树快照。

---

## 5. 验证四层

### L1 门层

| 项 | 证据 |
|---|---|
| 本门真树 | `GATE=GREEN session_provider_enabled` rc=0（8 个 `ProviderFactory.create` 站点仍解析富化模型） |
| 本门 selftest | `SESSION_PROVIDER_ENABLED_SELFTEST=PASS`（含新增臂：good=0 / pre=6 违规） |
| PRE 臂 | 基线树 rc=1，C-SEL-1 / C-SEL-2 / C-SEL-3 三条各中 |
| 真树变异 | 5 臂 `ALL ARMS MATCH`（m1..m4 各恰红其判据，m5 精度臂保持绿） |
| 全量门 | `gate_run.sh --full` → **`ran=111 pass=111 fail=0 sandbox=0 envretry=0 verdict=GREEN`** |
| 夹具 | `test_scan.py` → **`✅ ALL 340 CASES PASS`**；本门两行在册：`selftest has teeth (SESSION_PROVIDER_ENABLED_SELFTEST=PASS)` + `real tree green` |

> 沙箱症状自报：首次跑 `test_scan.py` 撞 S28（`OSError: [Errno 38] Function not
> implemented`，`subprocess._wait` 被打断），是**假红**不是夹具红。按注册表的健康路径
> 改走 `gate_run.sh --full`（全局 flock + ENOSYS 自动重试），`envretry=0` 全绿。
> 明细：`/var/minis/shared/tools/gate-runs/20261023T170558Z/summary.tsv`。

### L2 邻门层（8 道，全部 rc=0）

按**概念**（不是任务书点名的名字）：`chat_session_id_binding_check` ·
`chat_ui_interaction_check` · `provider_boundary_guard` · `provider_remember_key_check` ·
`thinking_wiring_guard` · `provider_write_rules_check` · `models_provider_field_check` ·
`provider_rpc_symmetry_check`

8/8 rc=0。`provider_remember_key_check` 的 `[C34]` 仍报
「1 member-resolution site(s), all consult the System single source」——本单新增的
`usableGroupMembers` 走的是同一个漏斗，没有另起一条凭据来源。

### L3 装置层（真 kotlinc + JUnit）

`ChatViewModel` 依赖过重无法 JVM 直编 ⇒ 按任务书的降级路径，把**决策本身**抽成
Android 无关纯函数（`GroupSelection.kt`），真 kotlinc 2.0.21 编译 + 真 JUnit 跑。

| 臂 | 期望 | 实测 |
|---|---|---|
| POST（真树 `GroupSelection.kt` + `GroupSelectionPlanTest.kt`） | 全绿 | **`OK (7 tests)`** |
| 真树违规数 | 0 | 0 ✔ |
| 变异臂 | 见下 | 见下 |

**编译**：`kotlinc-jvm 2.0.21 (JRE 17.0.18)`，
`-classpath junit-4.13.2:hamcrest-core-1.3`，rc=0，**0 警告**。

7 个用例逐条对应一个可独立失败的理由：未解析组不发布任何东西 / 未解析组不带 id /
未解析组即使有残留 id 也不发布（**这是最危险的那一格**：解析跑了但拒绝，
`_activeEntryId` 里躺着上一轮的 id，带进持久化就会绑到一个用户没点的成员）/
回退时交出真正服务的成员 / 已解析的 pick 才发布 / 解析没发布 id 时兜回 requested /
计划是纯值（两次调用一致，且 `effectiveEntryId != null` 与 `applyUi` 同真假）。

**变异臂 5 条**（施加在内存副本上，磁盘源文件不动）：m1 发布早于解析 / m2 持久化
原始 id / m3 绕过漏斗 / m4 rotation 跳过滤 / m5 解析器自身发布 —— 各恰红其判据
（详见 §4.4）。

**门判据的变异用 `probe/csel_mut.py`**：它在内存里做单点文本替换后重跑判据，
不落盘、不用 `git checkout --` ⇒ 不会把变异态留在工作树里。

### L4 CI 层

| 项 | 值 |
|---|---|
| run | `39993815841` |
| job | `120552040939` |
| attempt | 1 |
| conclusion | **success** |
| head_sha | `ba0b5f4cbdda9045e4a34779d0c16d78e1b5eaf5`（三方逐字符一致：本地 HEAD = `git ls-remote` = run.head_sha） |
| 非 success 步骤 | 仅第 23 步 `Publish to Releases` = **skipped**（main 门控，分支必 skip） |

**CI 日志未下载**：用两个 token 调 `actions/jobs/<id>/logs` 都返回 401
（该端点对当前 token 不放行），所以 L4 的判读只到 **steps API 结论层**，
没有做「日志文本级」判读（如门头序号连续、`Summary: 111 passed, 0 failed`、
`ALL 340` 逐条在册）。**这一层的缺口由 L1 的本地全量（111 门 + 340 夹具同树同门）
补上**，但它是**两条独立证据链**，不是同一条。

---

## 6. 残留与负决策

| # | 残留 | 触发条件（何时复活） | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| R1 | **真机行为级未验**：①点仅含系统语音哨兵的组 → 弹「仍然使用」后顶栏**不得**变；②组内排头成员无 key 时仍能落到下一个可用成员；③禁用 provider 的成员点选**不得**激活 | 下次真机窗口（与 `§1046-VERIFY` / `§1047-B1` 一次开齐） | 按 ①→③ 顺序，每条一次操作 + 一次顶栏/执行态对照 | 沙箱无 Android 运行时（Compose 重组 / `providerRepository` / 加密 prefs 都不可模拟） |
| R2 | `selectGroup` 失败时**静默**保持旧值，无可见反馈 | 用户报「点了没反应」 | 加一条可见提示（复用既有语言键或新增 ×7） | 当前签名返回 `void`、调用点有 4 处，语义改动超出本单半径；且「保持旧值」本身已经是正确行为 |
| R3 | `usableGroupMembers` 每次组解析读一遍 `loadApiKey`（同步加密 prefs I/O） | 组成员数大 / 用户报告组切换卡顿 | 缓存 `loadApiKey` 结果（`volatile` + instance id 键）或改异步读 | 与既有 `buildFallbackProviders:401` / `firstUsableGroupMember:909` 同构先例，成本量级相同；本单只把读取**提前**而非**新增** |
| R4 | C-SEL-1 的「块守卫」判据用**缩进**判定，理论上能被重排/压缩成同一行而失明 | 有人把 `if (resolved) { … }` 压成单行或改用 `resolved.takeIf{}` 形态 | 判据改为括号配平（需先解决 raw string 字面量污染） | 缩进判定在 kotlin 格式化下可靠；括号配平的成本要先解决 `"""{"type":…}"""` 污染，属另一单 |
| R5 | C-SEL-2 的 id 追溯只追 **3 跳**局部 `val` | 出现 4 跳以上的 id 传递链 | 跳数上限提到 5 或改成固定点分析 | 当前树最深 2 跳（`resolvedEntryId` ← `plan.effectiveEntryId`） |
| R6 | `selectEntry` 走漏斗后，`instance` / `apiKey` 改用 `decision.instance` / `decision.apiKey`（漏斗持有） | 漏斗将来做**归一化**（例如 trim key） | 无需动作 —— 这正是想要的：漏斗成为唯一真相 | — |
| R7 | L4 未做日志文本级判读（该端点 401） | 拿到可读 job 日志 | 按既有 `judge_ci.py` 判读器补判读 | 结论层（steps API）已达 attempt-1 success + 唯一 skipped 正确；文本层缺口由 L1 本地同树全量补 |
| R8 | `ProviderCredentialLiveSourceTest:206` 的 `loadApiKey` 计数断言只覆盖 `ProviderConnectionScreen.kt` | 有人给聊天层加凭据读取面 | 扩该断言的覆盖面 | 属别的门/别的单，本单只读不改 |

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `88ef9b2774cf14a0c983d5cb138db0e052ea7a83` |
| 本支 HEAD | `ba0b5f4cbdda9045e4a34779d0c16d78e1b5eaf5`（提交 `ba0b5f4c`） |
| 远端 | `refs/heads/fix/dv27-b2-group-selection-sync` = `ba0b5f4cbd…` |
| CI run / job | `39993815841` / `120552040939` |
| 三方 sha | 本地 HEAD = `git ls-remote` = `run.head_sha` = `ba0b5f4cbdda9045e4a34779d0c16d78e1b5eaf5`（逐字符） |
| 源码树 vs CI 绿树 | 同一提交（报告是**同一提交**里的纯文本新增，`fix-reports/` 不在 workflow paths 内） |
| 合并状态 | **未合并 main**（含 `.kt` 验证面 ⇒ 非纯文本例外，起手权在用户） |

---

## 8. 自证成本与不确定项

1. **本轮的两次自伤（均当场闭环）**：
   - ① 首版 `_fun_span` 把「当前函数自己的声明」当成下一个 `fun` 的起点 ⇒
     `_selectedGroupId` 的写入被判到了错误的函数里。改法：先收集全部 `fun` 起点，
     再取「≤ pos 的最后一个」与「> pos 的第一个」。
   - ② 首版 C-SEL-1 报 `ChatSessionLifecycle.kt` 里 `resolveProviderFromGroup`
     自身的发布为红 —— 循环论证。改法：显式豁免解析器自身（`_is_resolver`），
     并为此专门加了一条**精度臂 m5**（豁免过头就红）。
   - ③ `csel_block.py` 首版缺 `import re`（探针独立跑时报 `NameError`）。
2. **`_usable_filters` / `_resolution_names` 是树派生的** ⇒ 门对**重命名**不失明、
   对**删除**敏感。这是有意的取舍：改名不该让人重做门，删掉 helper 才是真回归。
3. **CI 日志未取到（401）** ⇒ L4 结论层已成立但缺文本层判读，详见 §5 L4 与 R7。
4. **真机行为未验** ⇒ §6 R1。
