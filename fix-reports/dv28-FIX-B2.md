# dv28-FIX-B2 — 模型选择器两处单源脱钩 + 门判据「存在但没挂上」（B-07 / P1）

- 分支：`fix/dv28-b2-model-picker-single-source`
- 基线：`eea9f407`（deep-audit-1047 并集后 main）
- 工单：B-07（P1）
- 独占生产面：`ui/chat/ChatModelPickerSheet.kt`　独占门：`scripts/scan/provider_remember_key_check.py`
- 停止节点：**本支 CI 绿**（未合并 main）

---

## 1. 缺陷与真树取证（全部行号/计数为本支实测，不采信报告）

### 1.1 折叠状态单源脱钩（形状 22）

基线 `ChatModelPickerSheet.kt`（1223 行）：

```
413:    val allInstanceIds = remember(config) {
414:        config.instances.filter { it.isEnabled }.map { it.id }.toSet()
415:    }
416:    var collapsedInstanceIds by remember(allInstanceIds) {
417:        mutableStateOf(allInstanceIds)
418:    }
```

- 单源 `ui/components/ModelEntryPicker.kt:136 fun rememberCollapsedInstanceIds(config, defaultExpanded = false)`；其种子 `providerInstanceIds(config) = config.instances.map { it.id }.toSet()`（`:97-98`，KDoc 原话「the identity of the instance list, and **nothing else**」）。
- 实测引用面：`grep -rn rememberCollapsedInstanceIds src/**/*.kt` ⇒ 定义 `ModelEntryPicker:136` + 调用 `AddModelsToGroupScreen:79` + `AgentLoopAddSheets:95` + 一处测试文件名断言（`QuickTestSheetContractTest:192`）。**picker 不在其中**。
- 缺陷的双重性质（本支新增的判据点）：即使把 `remember(allInstanceIds)` 认作「派生于 instance 列表」，`allInstanceIds` 仍带 `.filter { it.isEnabled }` —— 该旗标用户可在设置页随时切换，**切换即改变 remember 键 ⇒ 整个折叠集合重播种**，用户刚排好的展开/折叠状态被折回。这是「键派生自可变显示旗标」而不是「键与实例无关」，旧 C18 只判后者，故基线树在**挂载后仍然会绿**（见 §4.2 的 PRE 臂处理）。

### 1.2 系统语音成员副标题单源脱钩（形状 30 + 「判据存在 ≠ 判据挂上」镜像）

基线 `ChatModelPickerSheet.kt`：

```
840:  if (instance != null) {
841:      Text(instance.label.ifEmpty { instance.providerType.displayName }, …)   // 仅持久化实例
```

- 单源 `ui/settings/SystemVoiceMemberPresentation.kt:memberSubtitle(instanceLabel, isSystemEntry, systemLabel)`；调用点只有 `ModelGroupDetailScreen.kt:377-381`（另有 `:428` `canEditCostTier`、`:708` `removalDisplayName` 两个同对象方法）。
- System 语音成员**没有 ProviderInstance**（device 就是 provider，`SystemVoiceEntries.isSystemEntryId`）⇒ `find { it.id == entry.providerInstanceId }` 恒 null ⇒ 整段副标题不渲染。默认的 Voice Input / Voice Output 组**全部由 System 哨兵构成**，即 100% 命中。
- 结果：组详情页显示 `System · <modelId>`，同一成员在选择器展开时只剩裸 model id。

### 1.3 门的挂载面（本条的真正价值）

`scripts/scan/provider_remember_key_check.py`（基线 4609 行）：

| 判据 | 函数（实测行） | 基线挂载表（实测行） |
|---|---|---|
| C18 | `check_collapsed_screen` `:1404` | `(REL_ADD_MODELS, …)` `:2593`、`(REL_AGENT_LOOP_SHEETS, …)` `:2594` —— **无 picker** |
| C-SV2b | `check_system_voice_member_label` `:2518` | `(REL_GROUP_DETAIL, …)` `:2640` —— **无 picker** |

`grep -rln "ChatModelPickerSheet" scripts/scan/` ⇒ 仅 `provider_remember_key_check.py` 一处，所以两道判据的遗漏不是「别的门代管」，是**没人管**。

### 1.4 基线自证

`python3 scripts/scan/provider_remember_key_check.py .` ⇒ `GREEN`，rc=0（三门已全新对齐前的基线 0 违规）。

---

## 2. 修法（改动阶梯）

**停在第 2 级（复用既有单源函数），因为**：两处缺陷各自都已有正确的、被其它 2–3 个面在用、被独立 JVM 测试钉住的单源函数（`rememberCollapsedInstanceIds` / `SystemVoiceMemberPresentation.memberSubtitle`），本支要做的只是把第三个面接上去，没有任何新决策需要发明。

1. `:413-418` → `var collapsedInstanceIds by rememberCollapsedInstanceIds(config)`（保留 `var … by` 形态：既有形状测试读它；`defaultExpanded` 取默认 false，与改前「所有分区默认折叠」逐字同义）。
2. `:819-838` → 新增 `val memberSubtitle = SystemVoiceMemberPresentation.memberSubtitle(instanceLabel = instance?.label?.takeUnless { it.isEmpty() } ?: instance?.providerType?.displayName, isSystemEntry = SystemVoiceEntries.isSystemEntryId(entry.id), systemLabel = stringResource(R.string.voice_provider_system))`；`:861` 的渲染分支由 `if (instance != null)` 改为 `if (memberSubtitle.isNotEmpty())`。
   - 与 `ModelGroupDetailScreen:377-381` 逐字对齐（同名实参、同一 `isSystemEntryId` 判定、同一 `R.string.voice_provider_system`）；唯一保留差异 = 选择器原本对「空 label 的持久化 provider」回落到 `providerType.displayName`，本支用 `takeUnless { it.isEmpty() } ?: providerType.displayName` **保住**该回落，避免顺手改掉一个与本工单无关的显示行为。
3. **不做**（任务书划定的边界全部遵守）：未改 `rememberCollapsedInstanceIds` 实现、未改 `memberSubtitle` 判定、未动 `:491/:817` 的 `SystemVoiceEntries.resolveIn` 组员解析、未动 `:491` 的 `isCollapsed` 搜索门（形状测试锚点）、未在 picker 手写 `PROVIDER_DISPLAY_NAME` 字面分支。

---

## 3. 改动清单

3 文件 **+227 / −27**（生产 1 / 测试 1 / 门 1；报告另计；含 CI 红后对形状测试的收窄提交 `c993d48f`）：

| 文件 | 变化 | 说明 |
|---|---|---|
| `ui/chat/ChatModelPickerSheet.kt` | +32 / −8 | 两处委托 + `import rememberCollapsedInstanceIds` / `import SystemVoiceMemberPresentation`；删除私有 `allInstanceIds` |
| `ui/chat/ChatModelPickerSearchCollapseTest.kt` | +44 / −4 | 形状测试跟随播种形态（任务书 §3 明示允许）+ CI 红后的两处收窄（剥注释 / 过滤断言改「种子窗口域」，见 §5 L4） |
| `scripts/scan/provider_remember_key_check.py` | +151 / −15 | 挂载 2 行 + C18 形状判据 + C-SV2b 无读点消息 + `derivation_sources()` + 3 条带牙夹具 + 1 条 fail-closed 臂期望 |

门 **111 → 111**、夹具 **342 → 342**（0 新建门，连续第九波）；`scripts/scan/scan.sh` 与 `scripts/scan/test_scan.py` **零触碰**（冻结面）。

---

## 4. 门与判据（扩 `provider_remember_key_check.py`）

### 4.1 挂载补齐（两行）

```python
(REL_MODEL_PICKER_SHEET, check_collapsed_screen)          # C18
(REL_MODEL_PICKER_SHEET, check_system_voice_member_label) # C-SV2b
```

### 4.2 判据本身必须够硬——否则挂上也是假绿（本支最值钱的判断）

挂载后**先在基线树跑 PRE 臂**：

- **C18 挂上后对基线树是绿的**：旧判据问「键是否派生于 instance 列表」，`allInstanceIds` 确实派生于 `.instances` ⇒ 通过。判据对**形状 22 的第二种形态**（派生自可变显示旗标）失明。
  → 新增判据（仍记在 C18 名下、仍从函数体派生）：从键出发沿**声明链**（新工具 `derivation_sources()`，深度 ≤4，按名字解析而非拼写）取全部来源文本，若其中出现「`.filter`/`.filterNot` 且谓词读 `isEnabled` / `isHidden` / `pinned`」⇒ 红。理由写进 KDoc：折叠集合回答的是「provider 的**身份**」，用可变显示旗标过滤后，运行期切一次开关整集合就重播种。
- **C-SV2b 挂上后对基线树是红的，但红因是「零读点」**：基线 picker 里没有 `….?.label`（它用的是 NFC 的 `instance.label.ifEmpty{…}`，只在 `if (instance != null)` 内），判据落到 fail-closed 分支。
  → 把 fail-closed 的消息**具体化**为这条缺陷本身（「member rows 失去了 provider 副标题，或者把它读在 `if (instance != null)` 守卫里 —— 后者正是 System 成员只剩裸 model id 的形状」），让红灯指向修法而不是「请重新派生判据」。判据的轴未扩：仍要求「成员列表面上至少有把 provider 标签读经 System 单源」。

### 4.3 带牙臂（`--selftest`，臂数 126 → 129）

| 臂 | 构造 | 期望 |
|---|---|---|
| `c18-picker-inline-filtered-key` | 夹具里把委托换回 `remember(config)` + `filter { it.isEnabled }` | 恰红 C18 |
| `csv2-picker-member-label-guarded` | 把 `memberSubtitle` 换回 `if (instance != null) instance.label… else ""` | 恰红 C-SV2b |
| `csv2-picker-member-label-bare` | 保留 `instance?.label` 读点但去掉单源调用（`return instance?.label.orEmpty()`） | 恰红 C-SV2b |
| 既有 `clean` 臂 | 夹具改为委托形态（原来它自己就带着被禁止的私有副本） | 全绿 |
| 既有 `failclosed-sheet-missing-anchor` | picker 被重写成无关文件 | 期望集补 `C18`（新增挂载 ⇒ 新增 fail-closed 见证） |

---

## 5. 验证四层

### L1 门层（本支实测）

- 本门真树：`provider_remember_key_check — real tree: GREEN`，rc=0；C18 三条（ADD_MODELS / AGENT_LOOP / **picker**）全 PASS，C-SV2a/b 两条（GROUP_DETAIL / **picker**）全 PASS。
- `--selftest`：`PROVIDER_REMEMBER_SELFTEST=PASS`，**0 MISMATCH**（129 臂逐臂 `rc` 与 `failed=` 精确匹配）。
- **PRE 臂**（`probe/pre_arm.py`，字节级换入 `git show eea9f407:<picker>` → 跑门 → 原字节写回，sha256 对拍 `c05d008c…` 前后一致）：

```
provider_remember_key_check — real tree: RED
  [C18]    FAIL — keys=[allInstanceIds] resolve through a FILTERED instance projection
                  (`remember(config) { config.instances.filter { it.isEnabled }.map { it.id }.toSet() }`) …
  [C-SV2b] FAIL — the file lists group members but never reads a provider label through the
                  System single source … the label is read behind an `if (instance != null)` guard …
rc=1
```

  **恰好 2 条红，就是目标的两条**；其余 109 条判据在 PRE 树上全绿 ⇒ 判据只依赖这两处形态，没有顺手牵连。
- 全量门与夹具：本支在沙箱内三次尝试全量 `scan.sh`，前两次分别推进到 **54 / 66 门（全绿）** 后被沙箱回收（并发 8 个会话的 ENOSYS 风暴：`grep: Function not implemented`、`can't create /tmp/.minis_cwd_*`，`exit code -1`），第三次撞全局锁（`lock=busy`）未拿到。按 S28 纪律，**全量门与夹具的权威见证交由分支 CI**（L4）；本地只为本门 / 邻门 / 装置作见证。

### L2 邻门层（7/7 rc=0）

`grep -rln "ChatModelPickerSheet" scripts/scan/` 只命中本门，故按**概念**取邻门：

| 门 | 与工单的相邻面 | rc |
|---|---|---|
| `chat_ui_interaction_check` | chat UI 交互 | 0 |
| `models_provider_field_check` | provider 字段同步 | 0 |
| `session_provider_enabled_check` | **`isEnabled` 语义**（本支折叠键的旗标） | 0 |
| `stale_closure_guard` | 闭包/remember 过期 | 0 |
| `tts_voice_and_sanitizer_check` | 语音面（`--root` 形态） | 0 |
| `thinking_wiring_guard` | picker 调用点（位置参数形态） | 0 |
| `chat_session_id_binding_check` | chat 会话绑定 | 0 |

### L3 装置层（真 kotlinc + 真 JUnit，5 臂全 MATCH）

`probe/l3/`：`make_slice.py` 从**真树**机械抽取（`SystemVoiceMemberPresentation.kt` 整文件——它本来就是 Android-free 的；`ModelEntryPicker.kt` 的 `providerInstanceIds` / `collapsedInstanceIdsSeed` 两个函数按声明切片），只有 `ProviderConfig` / `ProviderInstance` 两个最小 stub 是手写的（唯一不可信面）。`run_arm.sh <arm>` 一次一臂（PRoot 单进程连续跑多臂会 ENOSYS）。

| 臂 | 结果 |
|---|---|
| `post`（真生产切片） | `OK (5 tests)`，rc=0 |
| `pre`（基线两种拼写：`if (instanceLabel != null) … else ""` + `filter { it.isEnabled }`） | `Tests run: 5, Failures: 2` —— 恰为 `a system voice member names the device as its provider` 与 `the shared seed covers every instance regardless of the enable flag` |
| `m1`（去掉 System 分支：`instanceLabel ?: ""`） | 恰红 1（System 成员） |
| `m2`（身份种子重新加上 enable 过滤） | 恰红 1（seed 覆盖 disabled 实例） |
| `m3`（陈旧条目冒充 System：`instanceLabel ?: systemLabel`） | 恰红 1（stale 条目应为空） |

期望值全部写成字面量（`"System"` / `"MyProvider"` / `""` / `setOf("enabled-provider","disabled-provider")`），不从被测常量推导；每处源串替换都断言 `count == 1`（1047-A3 教训：`.replace()` 源串是隐式契约，静默失配会造出假绿臂）。

### L4 CI 层

**三次 run，两次红都是本支自己的装置缺口，不是生产代码问题** —— 按「CI 绿 ≠ CI 干净」逐轮记：

| # | run / job | sha | 结论 | 真相 |
|---|---|---|---|---|
| 1 | `40049741612` / `120739547873` | `1ad9ecef` | **failure**（step 15 unit tests） | 本支新增的形状断言**过宽**：正则扫全文，命中了同文件里 `allInstancesWithEntries` 对 `isEnabled` 的**合法**过滤（它决定哪些分区存在），以及我自己写的解释性注释 |
| 2 | `40051683262` / `120745527390` | `c993d48f` | success，**但不干净** | 绿了，可本支改过的测试文件带 1 条警告：`ChatModelPickerSearchCollapseTest.kt:43:31 Java type mismatch: inferred type is 'kotlin.String?', but 'kotlin.String' was expected`（`System.getProperty("user.dir")` 是平台类型，直接喂 `File(String)`） |
| 3 | `40053325774` / `120750637360` | `a1c6099c` | **success 且干净** | attempt 1；唯一非 success 步骤 = 23 `Publish to Releases`（skipped，main 门控） |

**run 3 判读（日志 1,456,580 B，逐条核）**：`Summary: 111 passed, 0 failed`（8 条分块摘要全 0 failed，末条即全量）· `ALL 342 CASES PASS` · 门头 `[1/111]..[111/111]` 共 111 条且门名去重仍 111 · 编译错 `e: ` **0** · 含 `FAILED` 的行**全部**以 `PASSED` 结尾（0 条真失败，全是含 `FAILED` 字样的用例名）· 本门 `PROVIDER_REMEMBER_SELFTEST=PASS` 在册 · 形状测试 4 个方法逐条 `PASSED` · **本支改过的两个文件编译警告 0**（run 2 那条已消）。三方 sha 逐字符一致（本地 `a1c6099c35460192665969bd044b9c8a0e4d9091` == `git ls-remote` == run `head_sha`）。

**★ 两次红的可复用教训（同族，值得入规则库）**：
1. **形状测试的判据域要跟缺陷的域对齐**：缺陷在「播种的那几行」，判据就该只看播种窗口；全文扫描会同时误伤（a）同文件里语义不同但拼写相同的合法代码、（b）自己写下来说明缺陷的注释。修法两层：先剥注释（`codeOf`，与门里 `strip_kotlin_comments` 同源），再把过滤断言收进「种子声明 + 其上 6 行」。真正的**派生链**判定留给门（C18 + `DISPLAY_FLAG_FILTER_RE`，PRE 臂已验证恰红两条）。
2. **在 PRE 臂里跑不到的分支要有独立见证**：窗口判据在 PRE 臂里因 JUnit 在第一条断言就中止而永不可达 ⇒ 另写一条 python 直接对基线文件验证「正则确实命中种子派生区」（`run_test.sh` 尾部），否则这条齿就是**声明**而非**证据**。
3. **CI 绿之后仍要单独查警告**——本支 run 2 就是这么漏的（1042-A6 / 1045-FIX-8 / 1047-A1 同族第 N 次）。

---

## 6. 残留与失真

### 6.1 报告失真（任务书 vs 真树）

| # | 任务书写法 | 真树 | 影响 |
|---|---|---|---|
| 1 | `SystemVoiceMemberPresentation.memberSubtitle(entry, instance?.label?.ifEmpty { … })` | 真实签名是 **三参** `memberSubtitle(instanceLabel, isSystemEntry, systemLabel)`，没有 `entry` 形参 | 仅签名；「委托单源」的结论不变（按真树签名实现） |
| 2 | `check_c18_collapsed_ids_key`（TRIAGE §3 已记） | `check_collapsed_screen` | 仅命名 |
| 3 | 任务书 §1 称 picker 的 `rememberCollapsedInstanceIds` 引用「全仓 4 处」 | 定义 1 + 生产调用 2 + 一处测试断言文件名 | 计数口径差；「picker 不在其中」的结论成立 |

### 6.2 残留（全部带触发条件）

- **R1 真机行为级欠账**（本支未做）：打开模型选择器 → 切到 Voice Input / Voice Output 组 → 展开任一语音组，成员行应显示 `System · <modelId>`；再回设置页切换任一 provider 的启用开关，已展开的 provider 分区不得被折回、折叠的也不得被展开。**触发条件** = 下一次真机窗口。
- **R2 provider 分区 header 仍直接读 `instance.label`**（改后 `:1002` 一行；该 instance 来自 `orderedProviderSections`，恒非空，属于「provider 自己的分区标题」而不是成员行）。**触发条件** = 选择器的 provider 分区将来支持系统合成分组的那一天（届时该 header 也会遇到 null instance）。
- **R3 C18 的显示旗标是白名单**（`isEnabled` / `isHidden` / `pinned`）。**触发条件** = `ProviderInstance` 新增第三类「用户可变、与身份无关」的字段并被用作折叠/过滤键（届时把新字段加进 `DISPLAY_FLAG_FILTER_RE`）。
- **R4 C-SV2b 的站点识别只认 `….?.label`**；非安全调用形态（`if (x != null) x.label`）靠「零站点 ⇒ fail-closed 红」兜住，而非逐点判定。**触发条件** = 出现第三种标签读取拼写（如 `labelOf(instance)` helper）而该 helper 又不经单源。
- **R5 挂载表仍是人工枚举**（「哪个单源函数需要在哪些屏上挂判据」）。**触发条件** = 下一次 `provider_remember_key_check` 专项，或再出现第三对「单源函数 + 漏挂」。
- **R6 `defaultExpanded` 参数在 picker 未使用**（取默认 false）：选择器的语音组展开由组级 `expandedGroupIds` 单独管理，provider 分区仍默认折叠（与改前一致）。**触发条件** = 用户报告「语音组里的 provider 分区默认折叠不好用」。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 | `eea9f407`（`git log --oneline -2` 父链核实） |
| 提交 | `0a6b45a9` 生产 → `1ad9ecef` 门+测试 → `c993d48f` 形状测试收窄（CI 红后）→ `a1c6099c` 警告清零 → 报告提交（仅 `fix-reports/`，源码树等价） |
| 分支 | `fix/dv28-b2-model-picker-single-source` |
| CI run | **`40053325774`** / job `120750637360`（attempt 1，success；前两次 `40049741612` 红 / `40051683262` 绿但不干净，见 §5 L4） |
| head_sha | `a1c6099c35460192665969bd044b9c8a0e4d9091`（本地 == 远端 == run，逐字符） |

---

## 8. 自证成本与不确定项

- **自证成本**：本支两次门级装置（PRE 字节换入 + 129 臂自测）、一次 JVM 装置（5 臂 × kotlinc 编译），未做真机验证（R1 已登记）。
- **不确定项**：①「成员行副标题对空 label 的持久化 provider 回落到 `providerType.displayName`」是**选择器原有的行为**，本支刻意保住；组详情页对同一情形显示空 —— 两屏在这一个窄边界上仍不一致，按「半径纪律」未动。②C18 的新判据是**形状锁**（白名单旗标名），不是语义锁：它能抓住「用可变显示旗标过滤身份集合」这一类错，但抓不住「用一个语义等价但名字不同的推导」——把过滤行为藏进一个 helper 函数时，`derivation_sources()` 会跟进 helper 体（深度 ≤4），这条路是被覆盖的，只有「动态计算出的 id 集合」抓不到（当前无此形态）。
