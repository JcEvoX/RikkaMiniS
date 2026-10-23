# FIX-9 · deep-audit-1045（v24 波）· 支 9 · B-08（P0）+ B-05（P1）

- **BRANCH**：`fix/dv25-b5-fastmode-promote-draft`
- **基线**：`1d44ec905b8f5d21a215694e9a13910c74e1f645`（门 109 / 夹具 336）
- **停止节点**：分支 CI 绿，**不合并 main**

---

## §1 缺陷与根因（真树 Before 原文 + 实测行号）

### B-08（P0）· 草稿晋升与会话回收的竞态 ⇒ 用户输入 100% 蒸发

**Before（基线 `1d44ec90` 原文）**：

`src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatSessionEntryExt.kt:92-126`

```
 92  fun ChatViewModel.promoteDraftIfNeeded(): Boolean {
 93      if (!isDraft || realSessionId.isNotEmpty()) return false
 94      val text = _inputText.value
 95      if (text.isBlank()) return false
 ...
100      com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)
101
102      // ...
103      viewModelScope.launch {
104          val sid = ensureSession()
105          if (sid.isEmpty()) return@launch
106          val title = text.take(50).trim()
107          chatRepository.updateSessionTitleAndCategory(sid, title, null)
108          _sessionTitle.value = title
109          // The transcript row is the durable home for the body: `appendMessage`
110          // also refreshes `last_message` + `updated_at`, ...
112          val promoted = chatRepository.appendMessage(
113              sid, "user", buildUserPartsJson(text, emptyList()),
114          )
 ...
123          _inputText.value = ""
124      }
125      return true
126  }
```

`src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModel.kt:4328-4351`

```
4328  fun cleanupIfEmptyOnExit() {
4329      val sid = realSessionId
4330      if (sid.isEmpty()) return
4331      if (_isStreaming.value) return
4332      if (_attachments.value.isNotEmpty()) return
4333      viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
4334          try {
4335              val count = chatRepository.messageCount(sid)
4336              if (count > 0) return@launch
 ...
4341              chatRepository.deleteSessionRowOnly(sid)
4342              kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {
4343                  ChatViewModelStore.release(sid)
4344              }
```

**根因（一句话）**：晋升（promote）是**先释放草稿槽、再异步建行**的两段式，而退出清扫（cleanup）在晋升的第一条 `messages` 行落地之前就把这条新行当成「真空会话」删掉并 `release` 掉 ViewModel——`release` 取消的正是正在做晋升的那个 `viewModelScope`，此时用户的正文早已随草稿槽一起被清掉 ⇒ 正文与附件 100% 蒸发。

**证据链（三条，互相独立）**：

1. **时序**：`clearDraft`（:100）同步执行，`ensureSession()`（:104，插入 `sessions` 行、置 `realSessionId`）与 `appendMessage()`（:112，写第一条 `messages` 行）之间隔着 `updateSessionTitleAndCategory` 的 suspend。`cleanupIfEmptyOnExit` 的判据只有 `sid.isEmpty / _isStreaming / _attachments`——**没有「晋升在飞」这一条**。
2. **触发面**：`ChatScreen.kt:523` 的 `onDispose` 调 `cleanupIfEmptyOnExit()`；顶栏 New Chat（`ChatScreen.kt:2518-2520`）`promoteDraftIfNeeded()` 之后**立即** `onNewChat()` ⇒ 下一帧即 dispose ⇒ 落进上述窗口。抽屉路径（`:2164`）有 200 ms 关闭动画的宽限，所以只有顶栏路径 100% 触发。
3. **后果放大**：`ChatViewModelStore.release(sid)`（:4343）清掉 store ⇒ `ViewModel.onCleared` ⇒ `viewModelScope` 取消 ⇒ 晋升协程在 `appendMessage` 之前就被取消。而 `clearDraft` 已经把草稿槽清空、`_inputText` 只在 :123 才清（根本没执行到）——正文只剩内存里那份，而 VM 已被释放。

**次生缺陷（同一次晋升里）**：

- **附件丢弃**：:113 `buildUserPartsJson(text, emptyList())` —— 媒体引用列表恒为空，附件只贡献一个名字，不落 `mediaRef` part ⇒ 晋升后重载会话，图片/文件消失。
- **最新键入丢失**：:94 读 `_inputText.value`，而 composer 对 >8 字的 IME 突发（语音输入 / 拼音）在 `ChatInputArea.kt` 里缓冲 150 ms，缓冲期间正文只存在于 composer 的局部 `imeBurstBuffer` ⇒ 晋升的是**突发之前**的字符串。
- **失败即灭**：晋升协程若在 `appendMessage` 之前被取消/抛错，草稿槽已清、内存正文未落库 ⇒ 无任何副本。

### B-05（P1）· Fast Mode 开关的本地快照与实际生效值脱钩

**Before**：`ChatViewModel.kt:1859-1866`（基线）

```
1859      internal val _fastModeEnabled =
1860          MutableStateFlow(com.rikkaminis.app.data.FastModePrefs.isEnabled())
1861      val fastModeEnabled: StateFlow<Boolean> = _fastModeEnabled.asStateFlow()
1862
1863      fun setFastModeEnabled(enabled: Boolean) {
1864          com.rikkaminis.app.data.FastModePrefs.setEnabled(context, enabled)
1865          _fastModeEnabled.value = enabled
1866      }
```

**根因**：Fast Mode 是**应用级**持久化开关（`FastModePrefs`，prefs 名 `minis_fast_mode_prefs`），请求构建层每次请求直读 `FastModePrefs.isEnabled()`（`OpenAIProvider.kt:2940`）；而聊天界面读的是 **ViewModel 私有的、构造期取一次**的 `MutableStateFlow`。ViewModel 活在进程级 `ChatViewModelStore` 里 ⇒ 已经打开过的会话永远显示「该会话被创建时」的那份值。证据：文件里 KDoc 自陈「this flow only drives the menu row + nav badge」，而 `setFastModeEnabled` 只更新**本实例** ⇒ 在 A 聊天里切换、回到 B 聊天，菜单行与 ⚡ 徽标仍是旧值，**与实际发出去的请求不一致**。

---

## §2 修法（停在第几级 / 被否决方案）

### B-08（阶梯第 6 级：接既有单源 + 补同一个守卫）

根因假设已由上面三条证据支撑 ⇒ 修法 = **给晋升一个会话所有权位**，并把清扫的四个「不删」理由收敛到一个纯判据里。

1. **所有权位**：`ChatViewModel.promoteInFlight`（`@Volatile internal var`，`ChatViewModel.kt:855`）。晋升在**释放草稿槽之前**置位（`ChatSessionEntryExt.kt:132`），在 `finally` 里清位（:191）。**不是时间窗口**：晋升写库可能因 IO 被拖 arbitrarily 久，任何 `delay`/宽限都是把竞态平移而不是闭合（任务书明令禁止）。
2. **清扫接入**：`cleanupIfEmptyOnExit`（`ChatViewModel.kt:4378`）的四个早退合并成一个纯函数 `shouldSkipCleanup(...)`（新文件 `PromoteDraftPolicy.kt`），并在 IO 协程里**二次核对**该位（:4405）——清扫的判据读到的是「进入 IO 时」的世界，可能已经有晋升在飞。
3. **先落库后清草稿**：任务书建议把 `clearDraft` 移到 `appendMessage` 之后。**本实现否决该形状**（理由见下），改为：**草稿槽同步清、内存副本延后清**（`_inputText`/`_attachments` 在 `appendMessage` 成功之后才清），并加**失败恢复**（写回草稿槽）。
4. **附件同源**：晋升改走 `prepareUserAttachments(stagedAttachments, sid)`（与 `send()` 同一个纯核）+ `buildUserPartsJson(text, prepared.mediaRefPartsJson, prepared.attachedFilesXml)`。
5. **最新正文**：`composerTextForPromotion(_pendingComposerText.value, _inputText.value)`；`ChatInputArea` 在缓冲 IME 突发时 `publishPendingComposerText(tfv.text)`，在提交/取消/冲刷时置 null（`ChatInputArea.kt:395/1672/1695/1720`）。

**被否决的方案（逐条带理由）**：

| 方案 | 否决理由 |
|---|---|
| 把 `clearDraft` 移到 `appendMessage` 之后（任务书建议） | **槽位兼任 id 分配器**：`ComposerDraftStore.nextDraftId` 在槽位存在时**返回同一个 id**。延后清 ⇒ 下一次 New Chat 复用本 draft 的 id ⇒ 而那次延后到的 `clearDraft` 会把**新草稿的正文**抹掉。任务书该建议是**失真**（见 §6），本实现改用「同步清槽 + 失败写回」。 |
| cleanup 加延时/宽限期 | 任务书明令禁止；且延迟无界（写库可能任意慢），是平移竞态不是闭合竞态。 |
| 新建全局草稿缓存 | 违反「不改 `ComposerDraftStore` 数据契约」。 |
| 改 `ComposerDraftStore` 的 KV 契约 | 同上。 |
| 用 `MutableStateFlow` 取代 `@Volatile` 位 | `promoteInFlight` 是**一次性事件语义**的状态位，不是可观察值；写成 flow 会诱使后来者 `collect` 它，反而引入新的时序。 |
| 把 `shouldSkipCleanup` 写成 VM 内的私有函数 | 判据不可 JVM 化 ⇒ 无法 PRE/POST 双臂验证。抽成 Android 无关的独立文件是本仓既有做法（dv24-B05 的 `ComposerTextOps.kt`、dv24-b3 的 `isDrawerRowVisible`）。 |
| B-05：VM 内自己注册 `OnSharedPreferenceChangeListener` | 任务书允许，但 `FastModePrefs` 已经有进程级 `selfSyncListener` + `@Volatile cached`；VM 再注册一个 listener = **第三份表示**（cached / VM 副本 / listener 副本），正是缺陷本身的形状。改成「让全局对象暴露 flow、VM 直接读它」= 删掉副本而不是同步副本。 |

**修法一句话**：B-08 = 「给晋升一个会话所有权位 + 把清扫的四个理由收敛成一个纯判据 + 附件与最新正文同源 + 失败写回」；B-05 = 「删掉 VM 私有副本，改为读全局单源」。

---

## §3 改动清单（文件 + 增删行 + 行区间）

基线 `1d44ec90` → HEAD `a8f34b11`：**8 文件 +883 / −37**（不含本报告）

| 文件 | 增删 | 行区间 / 内容 |
|---|---|---|
| `src/.../ui/chat/PromoteDraftPolicy.kt` | +59 / −0 | **新增**。`shouldSkipCleanup`（:38）、`composerTextForPromotion`（:50）、`promoteTitle`（:58）。无 Android / Compose 依赖 ⇒ 真 JVM 可跑。 |
| `src/.../ui/chat/ChatSessionEntryExt.kt` | +125 / −27 | `promoteDraftIfNeeded` 全体重写（:106-224）：所有权位置位（:132）、同步清槽（:145）、`prepareUserAttachments`（:159）、`appendMessage` 带 mediaRef（:162-167）、`finally` 释放 + 失败写回当前槽主（:191-220）。KDoc 增补（:88-104）。 |
| `src/.../ui/chat/ChatViewModel.kt` | +82 / −7 | `_pendingComposerText`（:831）+ `publishPendingComposerText`（:834）、`promoteInFlight`（:855）、`cleanupIfEmptyOnExit` 改走纯判据（:4387-4395）+ IO 内二次核对（:4405-4412）、`fastModeEnabled` 改为全局单源（:1903-1913）。 |
| `src/.../ui/chat/ChatInputArea.kt` | +20 / −0 | IME 突发缓冲时发布待提交正文（:1672）、冲刷（:1695）/直接提交（:1720）/取消突发（:395）时置 null。 |
| `src/.../data/FastModePrefs.kt` | +35 / −2 | 新增进程级 `_enabled`/`enabled`（:63-77）、`publish()`（:82-86）、`selfSyncListener` 改为在监听体内重载快照再发布（:97-104）、`prime`/`setEnabled` 走 `publish`。 |
| `src/.../test/.../PromoteDraftPolicyTest.kt` | +138 / −0 | **新增**。9 个用例，全部驱动生产纯核，期望值写死字面量。 |
| `scripts/scan/skill_restore_session_cleanup_check.py` | +296 / −1 | 新判据 C3/C4/C5 + 6 个带牙臂。 |
| `scripts/scan/chat_ui_interaction_check.py` | +128 / −0 | 新判据 H1/H2 + 3 个带牙臂。 |

**冻结面 0 触碰**：`scripts/scan/scan.sh`、`scripts/scan/test_scan.py`、`.github/workflows/**`、`build.gradle*`、`AndroidManifest.xml`、8 份 `strings.xml` 均未出现在 diff 中（`git diff --name-only` 核对）。

**门数不变**：109 → 109（0 新建门，两道既有门各扩一组判据）。**夹具数不变**：336 → 336（`test_scan.py` 冻结，未动）。

---

## §4 门判据与带牙证明（逐臂「改什么 → 红在哪条」）

### 门 1 · `scripts/scan/skill_restore_session_cleanup_check.py`（独占）

新增 `check_promote_ownership(vm_src, entry_src)`，三条判据：

- **C3 — 清扫必须「读」所有权位**。判据用正则 `(?<![.\w])promoteInFlight(?!\s*=)`：**赋值不算读**。只认 `promoteInFlight` 这个字面会被两种写法骗过——`promoteInFlight = false` 硬编码假值（判据红但其实是写），`promoteInFlight` 只出现在赋值左侧（根本没人读）。
- **C4 — 清扫的判据必须走共享纯谓词** `shouldSkipCleanup(`。四个手写早退就是「第四个输入曾经漏掉」的形状本身；把判据收敛到一处，才有一处可被门看见。
- **C5 — 晋升必须「占位 + 释放 + 可恢复」**：必须有 `promoteInFlight = true`（占位）与 `= false`（释放，否则清扫被永久关掉，真空会话再也回收不了），且必须有 `ComposerDraftStore.saveText(`（失败时把正文写回草稿槽）。

**带牙臂（6 个，逐臂精确归因）**：

| 臂 | 改什么 | 期望红在哪条 | 实测 |
|---|---|---|---|
| 1 | 清扫的 ownership 输入硬编码为 `false`（谓词仍走共享路径） | C3（`never consults`） | 1 条 |
| 2 | 谓词改成四个手写早退（ownership 位仍读） | C4（`does not route its decision`） | 1 条 |
| 3 | 谓词改手写 + 去掉 ownership 位 | C4 + C3 | 2 条 |
| 4 | 晋升去掉 `promoteInFlight = true`（保留释放） | C5 占位 | 1 条 |
| 5 | 晋升去掉 `promoteInFlight = false` | C5 释放 | 1 条 |
| 6 | 晋升去掉 `saveText` 写回 | C5 可恢复 | 1 条 |
| 绿臂 | 合规形状 | 0 条 | 0 条 |

**精度证明**：臂 1/2/4/5/6 各自只红**恰好一条**判据（断言 `len(violations) == 1`），否则「只红该判据」不成立 ⇒ 任何一条判据单独坏掉都不会被别的臂掩盖。臂 3 允许红 2 条（它同时破坏两条）。

### 门 2 · `scripts/scan/chat_ui_interaction_check.py`（独占）

新增 `fast_mode_single_source_findings(vm_c, fm_c)`：

- **H1** — ChatViewModel 不得出现 `MutableStateFlow(... FastModePrefs.isEnabled() ...)` 形态的**构造期快照**。
- **H2** — `val fastModeEnabled: StateFlow<Boolean>` 的初始化必须来自 **FastModePrefs 暴露的那个公共 `StateFlow<Boolean>`**。判据是**派生**的：从 `FastModePrefs.kt` 里正则取出所有 `val <name>: StateFlow<Boolean>`，再检查 VM 的初始化是否引用其中之一 ⇒ 两侧任一侧改名都不失明，写死 `FastModePrefs.enabled` 则会在改名后误红。
- **H2 fail-closed** — FastModePrefs 读不到、或它**没有任何**公共 `StateFlow<Boolean>` ⇒ 判红（全局单源不存在，界面必然另开副本）。

**带牙臂（3 个）**：

| 臂 | 改什么 | 期望红 | 实测 |
|---|---|---|---|
| H1 | VM 还原成 `MutableStateFlow(FastModePrefs.isEnabled())` 快照 | H1 | ✅ |
| H2 | VM 改成本地 `MutableStateFlow(false)` 副本 | H2 | ✅ |
| H2 fail-closed | FastModePrefs 删掉 `val enabled: StateFlow<Boolean>` | H2 | ✅ |

**注**：H 家族要读 `ChatViewModel.kt`，故这三个臂的夹具树**必须**同时带上 `VM_F` 与 `INPUT_H`——否则臂会顺带触发 G6/G7 的无关判据，「只红该判据」的证明就作废了（本轮实测踩到，已闭环）。

### 邻门（L2，按**概念**反查 `grep -rln "ChatViewModel.kt\|ChatSessionEntryExt.kt\|FastModePrefs" scripts/scan/`）

| 邻门 | rc | 备注 |
|---|---|---|
| `prefs_listener_holder_guard.py` | 0 | **本轮被本改动触发过一次红**（见 §5 L1 记），已修 |
| `debug_leak_guard.py` | 0 | |
| `stale_closure_guard.py` | 0 | |
| `sysinfo_state_thread_check.py` | 0 | |
| `file_provider_paths_coverage_check.py` | 0 | |
| `models_provider_field_check.py` | 0 | |

---

## §5 验证四层结果

### L1 门

- `python3 scripts/scan/skill_restore_session_cleanup_check.py .` → **PASS**（rc=0）
- `python3 scripts/scan/skill_restore_session_cleanup_check.py --selftest` → **SKILL_RESTORE_SESSION_CLEANUP_SELFTEST=PASS**（含 6 个新带牙臂，全绿）
- `python3 scripts/scan/chat_ui_interaction_check.py .` → **CHAT-UI-INTERACTION GATE OK**（rc=0）
- `python3 scripts/scan/chat_ui_interaction_check.py --selftest` → **CHAT_UI_INTERACTION_SELFTEST=PASS (63 fixtures: clean + 62 violation/precision)**
- 全量 `sh scripts/scan/scan.sh` → **`Summary: 109 passed, 0 failed`**，门头 `[1/109]..[109/109]` 连续 ⇒ **门数仍 109**
- 全量 `python3 scripts/scan/test_scan.py` → **`✅ ALL 336 CASES PASS`** ⇒ **夹具仍 336**

**本轮的一次真实自伤（已闭环）**：`FastModePrefs.selfSyncListener` 改成调用 `publish(...)` 后，`prefs_listener_holder_guard.py` 的 **[ARCH-02]** 判红 ——「self-sync listener refreshes nothing（no `cached… =` assignment）」。该门认的是**监听体内存在 `cachedEnabled = …` 赋值**这一形状。修法 = 监听体内显式写 `cachedEnabled = changed.getBoolean(...)` 再 `publish(cachedEnabled)`。⇒ **规则级**：`publish()` 这类「统一写入口」的封装会让「直接写缓存字段」的门失明，封装不能吞掉字段赋值这一形状。

### L2 邻门

见 §4 末表：**6/6 rc=0**。

### L3 真跑（真 `kotlinc` + 真 `JUnitCore`）

装置：`probe/l3/arm.sh`（每臂独立一次 `sh` 调用，python 只做字节编辑，源树全程不动）。classpath 显式带 `junit:hamcrest` + `kotlin-stdlib`。

| 臂 | 形状 | 期望 | 实测 |
|---|---|---|---|
| POST | 修复树 | GREEN | `OK (9 tests)` |
| PRE | **基线形状**（三个手写早退、无 ownership 位；`composerTextForPromotion` 只取 committed） | RED | `Tests run: 9, Failures: 2` |
| M1 | POST 去掉 `|| promoteInFlight` | RED | `Failures: 1` |
| M2 | POST 去掉 `pendingBurst ?: committed` | RED | `Failures: 1` |

⇒ `ALL L3 ARMS MATCH`。PRE 臂的基线形状是**手写**的（基线树上没有这个纯核），语义与基线 `cleanupIfEmptyOnExit` 的三个早退逐条对应。

**为什么只覆盖这两个决策**：promote 的**时序**（谁先谁后）与**附件落库**需要 Room + Compose + 真机，JVM 化不了 ⇒ 由门（C3/C4/C5）+ §7 R1 真机欠账承担，**不硬造装置**。

### L4 CI

见 §8 表：**#2544 success**，三方 sha 逐字符一致，判读全绿（含 6 条 `FAILED` 子串的逐条排除、新类 9 条逐条在册、两门真树 + teeth 双源在册）。

**首跑被取消的一次**：`#2542`（`ad8e33de`）因我随后追加 `a8f34b11` 被 `concurrency` 取消——这是 workflow 的 `concurrency` 设计行为，不是失败；判读一律以与最终 HEAD 逐字符一致的 run（#2544）为准。

---

## §6 任务书失真

| # | 任务书原文 | 实测 | 处置 |
|---|---|---|---|
| 1 | 「`clearDraft` 移到 `appendMessage` 成功之后（保持『提升失败时草稿还在』）」 | **不可行**：草稿槽兼任 id 分配器（`ComposerDraftStore.nextDraftId` 在槽位存在时返回同一个 id）⇒ 延后清会让下一次 New Chat 复用本 draft 的 id，而那次延后到的 `clearDraft` 会抹掉**新草稿的正文**。 | 改为「同步清槽（id 语义不变）+ 内存副本延后清 + 失败写回草稿槽」，并在 `ChatSessionEntryExt.kt:137-144` 写明为何不是「先落库后清草稿」 |
| 2 | 「取文本用未防抖现值：promote 前 `commitComposerBody()` / `cancelPendingImeBurst()`」 | 这两个是 **composer 局部**的（`ChatInputArea.kt:388/406` 的 lambda），VM 侧拿不到；且 `cancelPendingImeBurst` 会**丢弃**缓冲内容。 | 改为 `ChatViewModel.publishPendingComposerText` 通道：composer 把待提交正文发布给 VM，晋升读它 |
| 3 | 「`buildUserPartsJson(text, _attachments.value)` 同款」 | 签名是 `buildUserPartsJson(text, mediaRefPartsJson: List<String>, attachedFilesXml: String?)` —— 第二参是**媒体引用 JSON 串列表**，不是 `InputAttachment` 列表 | 走 `prepareUserAttachments(attachments, sid)` 得到 `PreparedAttachments`，再取 `mediaRefPartsJson` / `attachedFilesXml` |
| 4 | 「`clearDraft` 移回前 → 红」作为带牙臂 | 若把 `clearDraft` 移回「`appendMessage` 之前」，反而与本实现「同步清槽」的形状**相同**（都在 `appendMessage` 前）——该臂无法区分两种正确形状 | 改判据为「失败时正文必须有可恢复的副本」（`saveText`），这才是「先落库后清草稿」想保护的东西，且对两种形状都成立 |
| 5 | 门锚点：`cleanupIfEmptyOnExit` 必须引用 promote 所有权位 | 任务书未指明「读 vs 写」 | 判据升级为**正则读判定**（赋值不算读），并补「占位/释放/可恢复」三条 |
| 6 | `promoteDraftIfNeeded` 的公开签名与返回值语义 | 未变：`fun ChatViewModel.promoteDraftIfNeeded(): Boolean`，返回「是否触发了晋升」 | 保持。仅新增「晋升在飞时返回 false」（同一草稿不会被晋升两次）——两个调用点都忽略返回值，行为不变 |

---

## §7 残留（R1..Rn，每条带触发条件）

- **R1 真机行为级**：顶栏 New Chat / 抽屉 New Chat / 返回键退出，各验「正文 + 附件都进新会话」。**本轮未验证**（无真机窗口）。验法：输入一段中文 + 挂一张图 → 点顶栏 New Chat → 打开刚建的会话 → 正文与图片都在；同一路径下用语音输入（IME 突发 >8 字）再验最新键入是否完整。
- **R2 真机行为级**：Fast Mode 开关在**已打开**的第二个会话里是否立即反映。验法：A 会话开 Fast Mode → 回 B 会话 → 菜单行与 ⚡ 徽标应已是开。
- **R3**：`promoteDraftIfNeeded` 失败恢复时，若新草稿槽**已被用户输入占用**，本实现**不覆盖**（只写回空槽）⇒ 那份正文随旧 VM 一起消失。触发条件：晋升失败**且**用户在失败后立刻在新草稿里打字。搁置理由：覆盖用户新输入比丢一份旧草稿更糟；改法需要草稿栈（数据契约变更，超出半径）。
- **R4**：`cleanupIfEmptyOnExit` 的 IO 内二次核对（`ChatViewModel.kt:4405`）与 `promoteInFlight = false` 之间仍有理论竞态：清扫读到 `false` 的瞬间，晋升可能正要开始（但那时 `realSessionId` 还空，`sid.isEmpty()` 挡得住）。触发条件：晋升在清扫的 IO 窗口内开始。搁置理由：该顺序下 `realSessionId` 尚未被 `ensureSession` 置位，首道守卫已挡；剩余窗口需要 CAS 式所有权，超出本轮半径。
- **R5**：`prepareUserAttachments` 会把附件**复制进**被晋升会话的 media 目录。若晋升在复制后失败，media 目录留下孤儿副本。触发条件：晋升中途失败。搁置理由：`deleteSessionRowOnly` 不删文件（既有约定：空会话的目录可能存着用户上传的文件），清理由既有孤儿清扫负责。
- **R6**：本轮改了 `FastModePrefs` 的写入路径（`publish`），但 `cachedEnabled` 仍是 context-free 读路径的来源。触发条件：有人在 `FastModePrefs` 之外直接写 prefs 文件。搁置理由：与既有 `selfSyncListener` 覆盖范围相同，本轮未缩小。
- **R7**：`promoteTitle` 固定 50 字符上限（沿用基线的 `text.take(50)`）。触发条件：用户草稿超过 50 字。搁置理由：既有行为，未扩大。
- **R8**：本轮门的新增判据**不校验** `promoteInFlight` 是否在 `ChatViewModel` 里是 `@Volatile`（跨线程可见性）。触发条件：有人去掉 `@Volatile`。搁置理由：门只认行为形状；`@Volatile` 的缺失由 Kotlin 编译器不报错、靠人守，成本与收益不划算（登记不修）。
- **R9**：`ChatInputArea` 的 `publishPendingComposerText` 在 4 个点发布/清空。触发条件：composer 新增一条写 `_inputText` 的路径而忘了同步发布。搁置理由：dv24-B05 的 `chat_ui_interaction_check` G6/G7 家族已覆盖「composer 写必须走 intent」，本条是同一族的延伸，但**不扩写别支的门**（半径纪律）。

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 | `1d44ec905b8f5d21a215694e9a13910c74e1f645` |
| 本地 HEAD | `94cf953ef90b7da834f104a979538430dc549123`（= CI 绿后追加的报告回填提交） |
| **源码树等价** | `git diff --name-only a8f34b11 HEAD -- . ':(exclude)fix-reports'` = **0 文件** ⇒ CI #2544 验过的树与当前 HEAD 的源码面**逐字节一致**，报告回填不动源码，不重跑 CI |
| 远端分支 | `fix/dv25-b5-fastmode-promote-draft` @ `94cf953e…`（`git ls-remote` 核对一致） |
| CI run | **#2544** / run `39917029875` / job `120293005852`，event=workflow_dispatch，attempt **1**，conclusion **success**；日志 1,412,119 B（`probe/ci/job-120293005852.log`） |
| CI head_sha | `a8f34b116654c83b1a9ce6ce6d23f79b04e7cb37` —— 与本地 HEAD、`git ls-remote` **逐字符一致** |
| CI 步骤 | 26 步，non-success = `[('Publish to Releases', 'skipped')]`（main 门控，分支构建不发版）✓ |
| CI 判读 | `Summary: 109 passed, 0 failed` 恰一条（另有 5 类子 Summary 全 `0 failed`）；`ALL 336 CASES PASS` 1 条；门头 `[n/109]` **109 个互异**；`Z e: ` 编译错 **0**；含 `FAILED` 的 6 行逐条确认**全是 PASSED 用例名子串**（`slot denied produces FAILED…` / `…maps to persistence_failed` / `phase FAILED maps to Failed` / `terminal FAILED maps to Failed` / `reason EXECUTION_FAILED…` / `legal FAILED terminal`）；本支新类 `PromoteDraftPolicyTest` **9 条逐条 PASSED**；两门真树 OK 行（log :703 / :920）与两门 teeth 行（:1181 / :1275）双双在册 |
| 提交序列 | `b1d00555` 生产(B-08) → `dfe6e9e3` 生产(B-05) → `c930e94a` 测试 → `ad8e33de` 门 → `a8f34b11` 失败恢复路径收紧（**CI 绿于此**）→ `94cf953e` 报告回填 |
| 门 / 夹具 | **109 / 336**（不变） |
| 改动规模 | 8 文件 **+883 / −37**（生产 5 文件 +274/−27、测试 1 文件 +138、门 2 文件 +424/−1） |
| 合并状态 | **未合并 main**（停止节点 = 分支 CI 绿；合并起手权在用户） |
