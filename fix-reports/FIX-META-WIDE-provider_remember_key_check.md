# FIX-META-WIDE-provider-remember-key — 名字还在，值没流

> wide-1024 宽扫第一波 · 支 01（provider-heavy）· 基线 `564c8f3e` · 分支 `fix/meta-wide-01-provider-heavy`
> 生产代码 0 行改动；`scan.sh` / `test_scan.py` 冻结面 0 触碰。

## 1. 门

`scripts/scan/provider_remember_key_check.py`（改写前 4745 行 → 改写后 5173 行，判据集 C1–C36）。

这道门守的是「UI 状态与配置/凭证之间的重派生契约」，本支涉及四条判据：

| 判据 | 它声称保证的事 |
|---|---|
| C1 | ProviderListScreen 的 `providerRows` 在配置或凭证变化时重算（行徽标 / 打了码的 Key 不冻结） |
| C30a/C30b | 凭证由效应经 IO 跳读取，且**该效应的产出被某个派生消费**（外部写 Key 后列表能看见） |
| C7b/C7c | 刷新后的 toast 报的是**刚读到的**条目数，不是刷新前的快照 |
| C13 | `ThinkingRuleResolver` 的每个 mutator 真的推进 revision（收集者能收到更新） |
| C33 | `QuickTestSheet.performTest` 送给 worker / 语音层的是**富化后**的模型 |

冻结断言：`PROVIDER_REMEMBER_SELFTEST=PASS`（`test_scan.py` 全等匹配，本支未改一个字符）。

## 2. 轴

四条见证分属两个失效轴：

| 见证 | 轴 | 旧判据锚在哪 | 为什么瞎 |
|---|---|---|---|
| C1 / C30b | **dataflow** | `'instances' in keys and 'modelEntries' in keys`；C30b 只查「生产者效应存在」 | 判据锚「两个特定名字在 key 列表里」，而**被读的那个值**是否在 key 里没人问；生产者存在 ≠ 产出被消费 |
| C7b / C7c | **dataflow** | `'allEntries' in args`；`'entriesFor(' in path` | 判据锚「窗口里出现过某个子串」，而 `.also { … }` 返回**接收者**、`.apply` 同理 —— 子串可以出现在被丢弃的计算里 |
| C13 | **dataflow** | `RESOLVER_BUMP_RE` 存在性 + 文件级 bump/write 计数 | 判据锚「有一行 `+= 1`」，没判**净增量**：下一行 `-= 1` 让 revision 回到旧值，`MutableStateFlow` 抑制等值发射 |
| C33 | **spelling** | `model = entry.model` + `(?!\s*\.)` 前瞻 | 前瞻只拒绝**裸载体**，`entry.model.copy()` 落进「后跟一个点」而豁免；且只有 CHAT 分支有正向判据，图片/语音/嵌入调用点只被「禁止某拼写」守 |

共同结构（与第一波 12 门同族）：**判据锚「某物存在」，不锚「该物的效果达成」**。

## 3. 见证（外部对抗审计席位产出，本机机械复证）

原始见证 4 条，逐条实体如下（`find → replace`，原样取自报告，未改写）：

| # | 文件 | 打穿的判据 | 补丁实质 |
|---|---|---|---|
| W1 | `ui/settings/ProviderListScreen.kt` | C1（连带 C30b 瞎） | `remember(instances, config.modelEntries, apiKeyByInstanceId)` → 去掉 `apiKeyByInstanceId`，块体里**仍在读**它 |
| W2 | `ui/settings/ProviderDetailScreen.kt` | C7b（连带 C7a/C7c 瞎） | `val total = providerRepository.entriesFor(instance.id).size` → `val total = allEntries.size.also { providerRepository.entriesFor(instance.id) }` |
| W3 | `provider/thinking/ThinkingRuleResolver.kt` | C13 | `setCustomRules` 体内 `_customRulesRevision.value += 1` 后追加 `-= 1` |
| W4 | `ui/components/QuickTestSheet.kt` | C33 | IMAGE_GEN 分支 `model = effectiveModel` → `model = entry.model.copy()` |

四条的缺陷后果（真机形态）：

- W1：从 ProviderDetail / `minis-config` 写入 API Key 后，列表行的凭证徽标与打码 Key 停在**首帧快照**，不再重派生（`instances` 与 `modelEntries` 都不动）。
- W2：刷新模型列表后的 toast 报的是**刷新前**的条目数 —— `entriesFor` 真跑了，结果被 `.also` 丢掉。
- W3：保存思考规则后 `_customRulesRevision` 净增量为 0，StateFlow 丢弃等值发射，ChatScreen / QuickTestSheet / ProviderDetail 三处收集者都收不到「规则已变」。
- W4：图片快捷测试把未富化的 `entry.model.copy()` 送进 worker，worker 侧没有 `ModelsDevApi.init`，无法重新推导模态 → 图片请求退化成 chat 请求（与 dv24/B-01 同一缺陷形态，只换了拼写）。

## 4. 修法（旧判据 → 新判据）

### 4.1 C1：key 覆盖「初始化器实际读到的状态载体」

旧：只验 `instances` + `modelEntries` 两个名字在 key 列表里。
新：从树派生 `state_carrier_names(code)` —— 委托式/`remember` 支撑的状态（`by remember { mutableStateOf }`、`by …collectAsState()`）、以及**生产者效应体内的赋值目标**；`val x = config.x` 这类纯值不算载体（它们由 C1 既有的两个具名规则覆盖）。然后要求：**初始化器读到的每个载体都必须是 key**。

~~~python
carriers = state_carrier_names(code) - {"providerRows"}
unkeyed = sorted(c for c in carriers
                 if re.search(r"\b%s\b" % re.escape(c), init) and c not in keys)
ok = ("instances" in keys and "modelEntries" in keys and not unkeyed)
~~~

这也是为什么改写后必须**同时**取用初始化器文本（`iter_remember_calls` 的 `init`），旧的单行 `remember_keys(c1_line)` 只看得见声明行。

### 4.2 C30b：补「生产者输出被消费」

旧：`bool(producers)` —— 效应存在即绿。
新：先收集生产者效应里的赋值目标（`targets`），再要求

1. 至少一个 `remember(...)` 初始化器**读到**该目标（否则「生产了没人消费」）；
2. 读到它的每个 `remember` 都把该目标列进 key（否则「消费但永不重派生」）。

W1 缺的正是第 2 条：消费点还在读，但不再是 key。

### 4.3 C7b/C7c：按「实参携带的值」判，剥掉丢弃型作用域函数

新增三个值语义原语：

- `_strip_passthrough(expr)`：删掉每个 `.also { … }` / `.apply { … }` 块（它们返回**接收者**，块内计算对值无贡献），字符串字面量与括号感知；
- `local_value_decls(code)` + `expression_value_chain(...)`：把实参**经本地绑定逐层展开**（深度上限 3），每层都剥掉落 passthrough；
- `composition_snapshots(code)`：`val x = remember(...) { … }` 的普通值 —— 闭包捕获，异步调用之后读到的是**调用前**的列表。

判据：

- **C7b**：任何 toast 实参的值链里出现「组合期快照且刷新路径未重赋值」⇒ 红；
- **C7c**：至少一个实参的计数**产生在刷新路径上**（实参本身含重读，或某个本地绑定在 `refreshModels`→toast 之间以重读赋值，或 state-backed 列表在路径上被重赋值且新值不再读旧值）。

W2：`total` 在路径上，但其值链剥掉 `.also` 后是 `allEntries.size` —— 一个组合期快照 ⇒ C7b 红；值链里已无重读 ⇒ C7c 也红（旧判据正是因为看见 `.also` 里的 `entriesFor(` 而判绿）。

### 4.4 C13：按净增量判

新增 `_revision_writes(body)`：解析体内**每一次**写 `customRulesRevision.value` 的形态 ——

- `+= <字面量>` / `++` → 记为正增量；
- `-= …` / `--` / `= …` / `*= …` 等 → 记为**问题**（可以把 bump 抵消，或无法证明净增量为正，fail-closed）；
- 净增量 ≤ 0 → 红。

W3 命中 `decrements it`：revision 回到旧值 ⇒ `MutableStateFlow` 等值抑制 ⇒ 三个收集者都收不到。

### 4.5 C33：从「禁止某拼写」改成「逐实参位正向要求」

旧的正向判据只有一句 `ModelsDevApi.effectiveModel(entry)` 在 `performTest` 体内出现过；反向判据是 `(?!\s*\.)` 前瞻。
新：

1. **载体层级**（`_QUICK_TEST_RAW_CARRIER_RE`）：`entry.model` / `entry.baseModel` 的任何成员访问都禁令，**只有 `.id` 豁免**（语音适配器把 `.id` 当模型/音色名，是 String，必须继续可用）；
2. **逐实参位正向**（`_QUICK_TEST_MODEL_ARG_RE` + `_argument_expression`）：`performTest` 体内每个 `model = …` / `resolvedModel = …` 实参，必须是
   - `<x>.model.id` / `<x>.baseModel.id`（String id，豁免），或
   - 引用 `effectiveModel` / `enrichedModel` / `ModelsDevApi.effectiveModel(…)` / `enrichModel(…)` / `providerModelFor(…)`；
   否则红并报出该实参文本；
3. 一个 `model =` 实参都没有 ⇒ **fail-closed 红**（不允许空转绿）。

真树立即：`performTest` 有 4 个模型实参位（文本 / 图片 / 语音合成 / 语音识别），全部落进上面的豁免或富化分支；W4 的 `entry.model.copy()` 在第一层就被抓，报出行号 600。

### 4.6 未触碰

- `src/android/**`、`src/shared/**`：**零改动**（`git diff --stat` 只有两个门文件）。
- `scan.sh` / `test_scan.py`：冻结面零触碰（`test_scan.py` 仍只断言 `PROVIDER_REMEMBER_SELFTEST=PASS`）。
- 既有判据的消息串（含 `capability reads=%d, single source present`）原样保留。
- 删掉唯一一处因本次改造而失效的死常量 `_QUICK_TEST_RAW_ARG_RE`（其语义已被新载体层级完全吸收）。

## 5. 四层证据

### 5.1 L1 本门 + 真树

~~~
$ python3 scripts/scan/provider_remember_key_check.py .
（无输出 → rc=0，真树全绿）

$ python3 scripts/scan/provider_remember_key_check.py --selftest
… 133 条 fixture 臂 …
  fixture c1-credential-key-dropped              expect_rc=1 rc=1 failed=C1,C30b      OK
  fixture c7bc-fresh-read-in-discarded-scope     expect_rc=1 rc=1 failed=C7b,C7c      OK
  fixture c13-cancelled-bump                     expect_rc=1 rc=1 failed=C13          OK
  fixture c33-quick-test-raw-carrier-copy        expect_rc=1 rc=1 failed=C33          OK
PROVIDER_REMEMBER_SELFTEST=PASS
~~~

新增 4 条臂（见证固化成 fixture，形状见 §8）：

| 臂 | 构造 | 期望 |
|---|---|---|
| `c1-credential-key-dropped` | clean 夹具去掉凭证 key，块体仍读它 | `C1,C30b` |
| `c7bc-fresh-read-in-discarded-scope` | clean 夹具把重读搬进 `.also` | `C7b,C7c` |
| `c13-cancelled-bump` | clean 夹具 `+= 1` 后 `-= 1` | `C13` |
| `c33-quick-test-raw-carrier-copy` | clean 夹具 `model = entry.model.copy()` | `C33` |

**两条既有臂的期望被修改**（语义正确，记录以防误读为回归）：

- `comments-do-not-count`：期望 `("C1",)` → `("C1","C30b")`。该夹具把 `val providerRows` 整行注释掉，文件里就**真的**没有任何消费者了 —— 新 C30b 的消费判据必须红。
- `c30-list-sync-read`：期望 `("C30a",)` → `("C30a","C30b")`。同步读让屏幕在组合期直接读 Key，生产者产出同样**无人消费** —— 同一缺陷的两个面。

clean 夹具 `FIX_PROVIDER_LIST_CLEAN` 的行构造同步成真树形状（`instances.map { instance -> instance to apiKeyByInstanceId[instance.id] }`），否则「消费」在夹具里根本不存在，新 C30b 会在 clean 臂上假红。

### 5.2 L2 见证反攻（唯一有效证据）

`probe/run_witness.py`：打补丁 → 跑**修复前的门**（`probe/baseline/` 里 `git show 564c8f3e:…` 的原文件）拿 `before_rc` → 跑修复后的门拿 `after_rc` → 恢复 → sha256 比对。

~~~
== provider_remember_key_check ==
  C1     before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  C7b    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  C13    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  C33    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
~~~

四条命中文本（证明抓的是**同一处**、且理由与见证语义一致）：

~~~
[C1]  FAIL — keys=[instances, config.modelEntries] — the initializer reads the state
       carrier(s) apiKeyByInstanceId that are not keyed …
[C7b] FAIL — 'total' still carries the pre-refresh snapshot allEntries
[C7c] FAIL — no argument of the toast reads a count produced ON the refresh path …
[C13] FAIL — setCustomRules bumps customRulesRevision but also decrements it
       (`customRulesRevision.value -= 1`) …
[C33] FAIL — the quick test hands the worker/voice layer a RAW entry carrier at line(s) 600 …
~~~

判据链成立：**同一把刀**修复前 rc=0（门瞎）、修复后 rc≠0（门抓住）；四次 restore 全部 sha256 一致，工作树零残留。

### 5.3 L3 邻门

| 邻门 | rc |
|---|---|
| `provider_import_id_roundtrip_check` | 0 |
| `models_provider_field_check` | 0 |
| `session_provider_enabled_check` | 0 |

### 5.4 L4 全量

~~~
$ bash scripts/scan/scan.sh
╔══════════════════════════════════════════════════╗
║  Summary: 112 passed, 0 failed
╚══════════════════════════════════════════════════╝
~~~

`test_scan.py` 的 344 条在本机单进程跑会撞沙箱缺陷 S28（第 220 个子进程起 `os.waitpid` 报 `ENOSYS`，与本支改动无关 —— `probe/baseline/` 的原门同样复现），因此按**一函数一进程**分块跑（`probe/chunk_driver.py`）：

~~~
前半 35 chunk：pass=241 fail=0（死点 test_da1021_gates 之后的 ENOSYS 由续跑补齐）
后半 11 chunk：pass=103 fail=0
总计          344 pass / 0 fail          ← 与冻结阈值 ALL 344 CASES PASS 逐数一致
~~~

46 个测试函数各跑一次、零失败、零 no-result（每个 chunk 的原始输出在 `probe/chunks/<fn>.log`）。

## 6. CI run

| 项 | 值 |
|---|---|
| run | `40136161820`（#2638） |
| job | `120998010635` |
| conclusion | **success**，attempt **1** |
| head_sha | `794ca82e7cf0186604a1ebe7676626aeb9574dea` —— 与本地 HEAD **逐字符一致** |
| 触发 | 手动 dispatch（分支 push 不触发 `build-apk.yml`，只认 main） |
| 26 步 | 全部 success；唯一 skipped = **Publish to Releases**（`if: refs/heads/main` 门控，预期） |
| 门步骤 | `Run pre-build scan gate` success |

job 日志逐条判读（`probe/ci/job.log`，1.48 MB）：

- `║  Summary: 112 passed, 0 failed` —— **恰 1 条**（其余 7 条 `Summary:` 是子套件：28/5/14/36/10/22/10 passed，全部 0 failed）；
- `ALL 344 CASES PASS` —— 恰 1 条；
- ` e: ` 编译错误行 —— **0 条**；
- `PROVIDER_REMEMBER_SELFTEST=PASS` / 本门自证标记 —— 在册（新增的 4 条 fixture 臂随 `--selftest` 一起被 CI 见证）。

报告提交发生在 CI 绿之后，源码树与绿 sha 等价：

~~~
$ git diff --name-only 794ca82e HEAD -- . ':(exclude)fix-reports' | wc -l
0
~~~

## 7. 残留（供主会话统一登记，本支未写 backlog / credibility）

| # | 残留 | 触发条件 | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| R1 | `state_carrier_names` 的声明识别是**逐行**的：`val x by⏎  remember { … }`（委托写在下一行）漏检 | 真树出现跨行委托声明 | 换成 `VAL_DECL_RE` + `statement_tail` 读取（C7 处已有同款实现，照搬即可） | 当前文件无此写法；改成跨行读取要重走一轮 selftest，收益为 0 |
| R2 | `expression_value_chain` 展开深度上限 3 | 出现「三次以上中转」的计数链路 | 提高上限或改成不动点迭代 | 上限是为了防环；真树最深 2 跳（`total` → `allEntries.size`），再深属臆测 |
| R3 | C7 对 `.let { it.size }` 视为「值里含快照」而判红（保守） | 有人把计数改写成 `entriesFor(id).let { it.size }` 之外的、以快照为接收者的 `.let` | 给 `.let` 加「lambda 参数替换」语义（把 `it` 换成接收者文本后再判） | `.let` 返回值来自 lambda 体，语义替换容易做错；方向安全（不静默放行），先保守 |
| R4 | C13 对非字面量增量（`+= STEP`）fail-closed 红 | 有人把步长提成具名常量 | 解析 `val STEP = <int>` 常量化后再判净增量 | 现在只有 `+= 1`；常量折叠属 §2 的下一层装置，本轮半径外 |
| R5 | C30b 的「消费者」只认 `remember(...)`；`derivedStateOf` / `LaunchedEffect(keys)` 收集点不认 | 有人把凭证消费改成 `derivedStateOf` | 把消费载体集合扩成 `remember` ∪ `derivedStateOf` ∪ 带 key 的 `LaunchedEffect` | 真树只有 `remember`；且未识别时方向是**红**（fail-closed），不会静默放行 |
| R6 | C33 的半径 = `performTest` 函数体；别的执行入口（如批量测试面板）不在内 | 新增第二个快捷测试入口 | 按「调用 ProviderExecutionGateway / voice 合成的函数」派生入口集（同第一波 `_body_opener` 手法） | 当前只有一个入口；派生入口集需要先有解析器基础设施 |

## 8. 复现命令

~~~bash
cd /var/minis/shared/work/meta-wide/wt-01-provider-heavy

# L1 本门（真树 + 自证臂）
python3 scripts/scan/provider_remember_key_check.py .
python3 scripts/scan/provider_remember_key_check.py --selftest

# L2 见证反攻（补齐 before/after 双读数：修复前门来自 probe/baseline/）
python3 probe/run_witness.py provider_remember_key_check

# L3 邻门
for g in provider_import_id_roundtrip_check models_provider_field_check session_provider_enabled_check; do
  python3 scripts/scan/$g.py . >/dev/null; echo "$g rc=$?"; done

# L4 全量
bash scripts/scan/scan.sh | tail -3
python3 probe/chunk_driver.py          # 单进程版会撞 S28，见 §5.4
~~~
