# FIX-A2 — deep-audit-1047 A-02（`:modelservice` 非流式路径漏读 `thinking_level`）

> 支 A2 / 工单 A-02（P1）/ 分支 `fix/dv27-a2-modelsvc-thinking-level` / 基线 main `88ef9b27`

---

## 1. 缺陷与真树取证（基线 `88ef9b27`）

| 位置 | 事实 |
|---|---|
| `ModelExecutionDispatcher.kt:57` `buildRequestJson` | 两条路由**共用**同一 body 构造；`:299` `if (thinkingLevel != ThinkingLevel.OFF) put("thinking_level", thinkingLevel.name)` —— 非 OFF 档位无条件序列化 |
| `ModelExecutionService.kt:1335-1338`（基线，流式） | `req.optString("thinking_level", "").let { if (it.isEmpty()) OFF else safeEnum(it, OFF) }`，`:1491` 传 `provider.streamMessage(..., thinkingLevel = thinkingLevel)` |
| `ModelExecutionService.kt:1134-1146`（基线，非流式） | `provider.sendMessage(messages, systemPrompt, maxTokens, temperature, imageParts, tools)` —— **全函数 0 次读 `"thinking_level"`**，无 `thinkingLevel =` 实参 |
| `LLMProvider.kt:84` | `sendMessage(..., thinkingLevel: ThinkingLevel = ThinkingLevel.OFF)` ⇒ 缺省即 **OFF** |

后果：`minis-model-use run --thinking high`（不带 `--stream`）、子代理 / 后台非流式请求，在 DeepSeek / Qwen3 / Claude 3.7+ 上**显式关闭**模型默认推理。dispatcher 写了、worker 丢了、provider 用默认值兜底 —— 三处各自"合理"，链路上无人负责。

**同族对称性复核（程序化，非人工列举）**：从 `buildRequestJson` 派生 35 个请求级键，在 service 全文逐键找读点，**0 个孤儿**；两条 provider 调用的具名实参面在修复后逐项相同（7 项）。`thinking_level` 是唯一"被写、被一侧读、另一侧丢"的键。

---

## 2. 修法（走改动阶梯）

**停在第 6 级（能一行就不造结构），因为**：

- ①真要做吗：要。P1，触发路径明确（任何非流式请求 + 非 OFF 档位），后果是静默改变模型行为。
- ②代码库里已有：**有**——流式路径的解析就在本文件里，修法不是新逻辑而是"让两条路径共用同一份"。
- ③标准库 / ④平台原生 / ⑤已装依赖：无需新增。
- ⑥能一行：不能。两份副本必须收敛成**一个**入口，否则第三次分叉只是时间问题（本缺陷本身就是第二次分叉的产物）。
- ⑦最小实现：**新增 1 个 Android 无关纯函数文件 + 2 处调用点各 1 行 + 1 个异常类挪位 + 1 个纯 JVM 测试 + 扩 1 道既有门**。

关键决定：

1. **解析抽到 `ThinkingLevelWire.kt`**（`internal fun thinkingLevelFromWire(raw: String)`），两条路径都只写 `thinkingLevelFromWire(req.optString("thinking_level", ""))`。语义与基线流式分支**逐字节等价**：空串 ⇒ `OFF`（dispatcher 刻意省略 OFF 键），非空未知 ⇒ 仍抛 `UnknownEnumValueException`（跨版本严格契约不变）。
2. **`UnknownEnumValueException` 从 `ModelExecutionService.kt` 挪到独立文件**（内容一字未改，KDoc 追加一段说明）。动机是证据质量：`ModelExecutionService.kt` 拖 Android `Service`/`Base64`，纯 JVM 编不动，解析函数留在里面就只能靠文本判据；挪出来后 L3 能**真编译真跑**生产代码。
3. **刻意不动**：生图分支（`ImageGenCapableProvider` 接口不收 `ThinkingLevel`）、dispatcher 的 `!= OFF` 省略条件、`restoreThinkingRules(req)` 位置与语义、非流式响应 JSON 形状。

---

## 3. 改动清单

| 文件 | 行数 | 性质 |
|---|---|---|
| `sandbox/offload/ThinkingLevelWire.kt` | 41（新） | 唯一解析入口（Android 无关） |
| `sandbox/offload/UnknownEnumValueException.kt` | 22（新） | 从 service 挪出，内容不变 |
| `sandbox/offload/ModelExecutionService.kt` | +12/−18 | 两条路径接入共享解析（`:1138`/`:1153` 非流式，`:1343`/`:1496` 流式），异常类定义移出 |
| `src/test/.../ThinkingLevelWireTest.kt` | 61（新） | 5 个 JVM 用例：空串→OFF、全档位 round-trip、AUTO、未知抛错、小写不宽容 |
| `scripts/scan/modeluse_proto_single_source_check.py` | +353/−2 | C1/C2 判据 + 4 个带牙臂 + 1 个合规夹具 |

**门数不变（111）· 夹具不变（340）· `scan.sh` / `test_scan.py` 零触碰**（新判据全部长在既有门里）。

---

## 4. 门与判据（扩 `scripts/scan/modeluse_proto_single_source_check.py`）

### C1 —— 派生键不得成为孤儿（`check_service_wiring` 前半）

从 `buildRequestJson` 派生请求级键：取**最外层** `JSONObject().apply { }` 内的 `put("…"`，排除嵌套 builder（message / contentPart / tool-schema 字段不是请求级）；函数体先做分参数括号再找花括号，避免 `JSONObject().apply {` 被误当函数体。派生结果 **35 键**，要求每个键在 service 全文有 `opt*/get*/has("键")` 读点。

### C2 —— 档位必须落到调用，且两条路径实参面相同

- **C2a**：`executeRun` 的 `provider.sendMessage(` 与 `executeStreamingRun` 的 `provider.streamMessage(` 都必须带 `thinkingLevel =` 具名实参；
- **C2b**：该实参的值必须能追溯到**同一函数体内**对 `thinking_level` 的读取（允许一跳 helper：`val x = helper(req.optString("thinking_level",…))`）。常量（`ThinkingLevel.OFF`）与小写字面量都无法通过 —— 它们不产生读点；
- **C2c**：两条调用的具名实参**集合**必须相等（`_named_args` 逐项比对），任何一侧丢参数即红 —— 这条覆盖的正是本波与历史上 `tools` / `imageParts` 两次"单侧丢参"。

### 带牙臂（`--selftest`，每臂断言**恰好一条**违规 + 命中指定判据）

| 臂 | 形态 | 期望 |
|---|---|---|
| ① | 非流式路径不解析（实参值未绑定） | 红 C2b，且仅此一条 |
| ② | `thinkingLevel = ThinkingLevel.OFF` 常量 | 红 C2b，且仅此一条 |
| ③ | dispatcher 新增 `put("nobody_reads_me")`，两侧都不读 | 红 C1，且仅此一条 |
| ④ | 非流式调用丢掉 `maxTokens =` | 红 C2c，且仅此一条 |
| 合规夹具 | 两条路径共用解析 + 实参面相同 | 全绿 |

`--selftest` 实测输出：

```
MODELUSE_PROTO_SELFTEST=PASS
MODELUSE_PROVIDER_MATCHER_SELFTEST=PASS (4 fixtures: 3 violations caught, 1 fail-closed)
```

### ★ 与任务书原文的偏离（必须写明）

任务书 C1 写的是「派生键在 `executeRun` 与 `executeStreamingRun` 两个函数体内**各自**都能找到消费」。按字面实现会在**干净树**上红：`input_json` / `passthrough_request` / `output_ext` 是**非流式专属**路由（生图 / 原始透传），`streaming` 在两条 body 之前就决定了路由（`:402` 读）。一个在正确代码上必红的判据无法被信任，因此 C1 取 **service 级「无孤儿键」**（覆盖任务书的真实意图：第三处新增字段漏读会被抓），把"逐路径对称"交给 C2c 在**调用点**判定 —— 那里丢参数没有歧义。

**PRE 臂**（`probe/pre/`：基线 `ModelExecutionService.kt` + 真树 dispatcher / handler / protocol）：

```
modeluse_proto_single_source_check: 2 violation(s)
  ModelExecutionService.kt: `executeRun` calls provider.sendMessage without `thinkingLevel =` — …
  ModelExecutionService.kt: `executeStreamingRun` passes `thinkingLevel =` to provider.streamMessage but `executeRun` does not — one path drops an argument the other hands over
rc=1
```

---

## 5. 验证四层证据

**L1（门 + 全量）**
- 真树 `modeluse_proto_single_source_check.py .` → `clean`，rc=0
- `--selftest` → rc=0（4 脏臂各恰红其一 + 合规夹具全绿，见上表）
- PRE 臂 → rc=1，两条违规精确指向 `executeRun` 丢档位 / 丢参数（输出见上）
- 全量 `scan.sh` → **`Summary: 111 passed, 0 failed`**，门头 `[1/111]..[111/111]` **111 个互异**
- `test_scan.py` → **`✅ ALL 340 CASES PASS`**，`^(FAIL|ERROR)` 计数 **0**

**L2（概念邻门 6 道，全 rc=0）**：`modelsvc_single_read_check` / `offload_media_b64_wire_check` / `provider_boundary_guard` / `xproc_boundary_check` / `offload_cli_args_check` / `thinking_wiring_guard`。

**L3（真 kotlinc 2.0.21 + 真 JUnitCore，`probe/l3/run_l3.py`）**
被测对象是**生产文件本身**：`ThinkingLevelWire.kt` + `UnknownEnumValueException.kt` + 仓库里新增的 `ThinkingLevelWireTest.kt`。唯一合成件是 `ThinkingLevel` 枚举切片 —— 从 `ProviderConfig.kt:84` **逐字提取**（含 `AUTO` 与 `decoded()`），仅去掉 `@Serializable`（只影响序列化，与 `valueOf` 无关，否则需要编译器插件）。

| 臂 | 结果 |
|---|---|
| POST（生产解析 + 真测试） | `OK`，5 用例全绿 |
| m1 删空串守卫 | 红 `absentKeyDecodesAsOff`（**恰好这一个**） |
| m2 未知值静默回落 OFF | 红 `unknownValueThrowsInsteadOfSilentlyDegrading` + `lowercaseIsNotSilentlyAccepted`（恰好这两个） |
| m3 恒返回 OFF | 红 `everyKnownLevelRoundTrips` + `appendedAutoCaseRoundTrips`（恰好这两个） |

`L3 RESULT: ALL ARMS MATCH`。附加证据：首个 POST 轮次全红是因为装置漏了 `kotlin-stdlib` 在**运行时** classpath（`NoClassDefFoundError: kotlin/jvm/internal/Intrinsics`），不是被测代码问题；补 `/opt/kotlinc/lib/kotlin-stdlib.jar` 后转绿（记录在 §8）。

**L4（CI）**：**run `40002574114` / job `120582199256` / run #2578 / attempt 1 / event `workflow_dispatch` / conclusion `success` @ `6fe519f8`**（三方 sha 逐字符一致：本地 == `run.head_sha` == `ls-remote`）。26 步，**坏步 0**，唯一 `skipped` = `Publish to Releases`（main 门控，符合预期）。日志 1,444,024 B 判读：`Summary:` 行 8 条**全部 0 failed**（含 `║  Summary: 111 passed, 0 failed`）· 门头 **111 个互异**（`[1/111]..[111/111]`）· `✅ ALL 340 CASES PASS` · 编译错 `e:` **0** · **本支 4 个文件的诊断（`e:`/`w:`）0 条** · 5 条新用例逐条 `PASSED`（`absentKeyDecodesAsOff` / `everyKnownLevelRoundTrips` / `appendedAutoCaseRoundTrips` / `unknownValueThrowsInsteadOfSilentlyDegrading` / `lowercaseIsNotSilentlyAccepted`）。
判读纪律：run/job/step 结论取自 **API JSON**（`run.conclusion` / `steps[].conclusion`），日志只用于确认形状（`Summary:` / 门头 / `ALL 340`），不从日志文本猜步骤是否 skipped —— 1042-B5 的同族教训。

---

## 6. 残留与负决策（全带触发条件）

- **R1（P1，真机）** `minis-model-use run --thinking high`（**不带** `--stream`）时上游请求体确实携带思考参数，且模型输出出现推理内容。**触发条件** = 下次真机窗口（与 `§1046-VERIFY` / `§1047-A4` R1 一次开齐）。**触发后方案** = 一次非流式调用 + 抓上游请求体/日志对照。**搁置理由** = 沙箱无 Android 运行时，跨进程链路无法在 JVM 内复现。
- **R2（P2）C1 是 service 级、不是逐路径级**（见 §4 偏离说明）。**触发条件** = 出现"某键必须两条路径都读、但只有一条读"且 C2c 抓不到的情形（即该键不进入 provider 调用实参）。**触发后方案** = 把 C1 升级成"按路由分支标注哪些键属于哪条路径"，需要 dispatcher 侧显式标注。**搁置理由** = 当前四条单路径专属键（`input_json`/`passthrough_request`/`output_ext`/`streaming`）都有结构性理由，硬要求双侧消费会制造假红。
- **R3（P3）生图分支不收 `ThinkingLevel`**（`ImageGenCapableProvider` 接口未声明该参数，属刻意）。**触发条件** = 图像生成模型开始支持推理档位。**触发后方案** = 接口加参数 + 本门 C2 自动扩展到第三条调用。**搁置理由** = 平台接口未提供该能力。
- **R4（P2）C2 只认每个 entry 里**第一个** `provider.<method>(` 调用**。**触发条件** = 出现同函数内多 provider 回退调用（fallback 链下沉到 worker）。**触发后方案** = 遍历全部调用点，逐个判定实参面。**搁置理由** = 当前 worker 每个 entry 只有一次 provider 调用（fallback 在 app 侧）。
- **R5（P2）C2c 的对称判据假定两方法参数面一致**。**触发条件** = `sendMessage` 与 `streamMessage` 的签名分叉（新增流式专属参数）。**触发后方案** = 从 `LLMProvider.kt` 两个签名派生交集，只对交集判对称。**搁置理由** = 当前两签名逐参数相同（7 项）。

---

## 7. 身份链

基线 `88ef9b27` → 生产+测试 `9768b220` → 门 `0c64bdf1` → 报告 `6fe519f8`（CI 绿于此）→ 报告回填（本条提交）。

- **CI 验过的代码树 = `6fe519f8`**：run `40002574114` / job `120582199256` / #2578 / attempt 1 / `conclusion=success`。
- **报告回填提交只改 `fix-reports/`**（不在 `build-apk.yml` 的 `paths` 内、不参与构建），因此 `git diff --name-only 6fe519f8 HEAD -- . ':(exclude)fix-reports'` 必须为**空** —— 即 CI 结论对当前代码树成立（1042-A5 确立的「源码树等价」判据，避免为回填反复重跑 CI）。
- 三方 sha 逐字符一致（本地 HEAD `6fe519f8` == `run.head_sha` == `ls-remote origin fix/dv27-a2-modelsvc-thinking-level`）。

---

## 8. 自证成本与不确定项

- **装置成本**：5 个文件、约 490 行（生产 63 / 测试 61 / 门 353）。对 P1 + 静默改行为的缺陷，装置比例合理；未新建门、未碰冻结面。
- **不确定项**：
  1. **L3 的枚举切片**是唯一合成件。取的是逐字提取 + 去注解，但它们不是同一个编译单元 —— 若某天 `ThinkingLevel` 增加需要插件的成员，切片会先炸。已在报告中标注，不隐藏。
  2. **C1 的派生依赖 dispatcher 的写法**（`put("…")` 字面量键）。若改成 `put(keyVar, …)` 动态键，派生会返回 0 键 → 门 fail-closed 报错（`no request-level put(… could be derived`），不会静默放过 —— 这是刻意设计。
  3. **本轮未做真机验证**（R1），跨进程链路只有文本判据 + 单侧 JVM 真跑支撑。
- **本轮自伤（已闭环，形态可复用）**：
  1. **L3 首轮装置假红**：`java -cp` 只给了 junit/hamcrest，缺 `kotlin-stdlib.jar` ⇒ 5 个用例全报 `NoClassDefFoundError`。教训：**kotlinc 编译通过 ≠ 运行时能加载**，JVM 臂的 classpath 要分编译期/运行期两份对齐。
  2. **门判据的"字面实现"会假红**：任务书 C1 的字面口径在干净树上必红（§4 偏离说明）。教训与 1046 波同族：**判据先在不变量上验证一遍干净树再落地**，否则门本身成为噪音源。
  3. **派生键提取两次返回 0**：先是 `\.put(` 漏了 `apply` 内不带点的 `put(`，后是"排除嵌套 builder"把最外层也排掉了。教训：**解析器要先用真文件跑出预期基数再嵌进判据**，不能凭想象写正则。
