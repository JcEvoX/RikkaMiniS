# FIX-META-WIDE-modeluse_proto_single_source — 门见证加固（wide-1024 支 07）

## 1. 门

`scripts/scan/modeluse_proto_single_source_check.py` — `minis-model-use` 协议单源（方言委托 / 富化模型下发 / thinking_level 到达两条 provider 路径），基线 `564c8f3e`。

## 2. 轴

| 见证 | 轴 | 判据锚缺陷 |
|---|---|---|
| C2（`_traces_to_level`） | dataflow | `re.search` 只证明读**被提到**，不证明读出的值**被采用** |
| Rule 7（`check_enriched_dispatch`） | dataflow | 别名从整行收集，不判绑定是否**停在**富化调用 |
| Rule 1（`is_delegate_def`） | dataflow / anchor | 3 行窗口含 `ModelUseProtocol.` 即算委托，不判函数体 |

## 3. 见证（9 条实锤中本门的 3 条，全部已机械复证）

1. **C2**：`val thinkingLevel = thinkingLevelFromWire(req.optString("thinking_level", ""))` 行尾追加 `.let { ThinkingLevel.OFF }` → 旧门仍绿。后果：所有非流式 offload 请求把思考等级强制成 OFF，模型被要求不要推理（正是 C2 存在的那个缺陷）。
2. **Rule 7**：`val effectiveModel = …effectiveModel(entry)` 追加 `.let { entry.model }` → 旧门仍绿（类型完全兼容）。后果：模型覆写 + models.dev 能力字段在进程边界全部丢弃。
3. **Rule 1**：`ModelUseProtocol.parseCustomEndpointPath(inputJson)` 追加 `?: JSONObject(inputJson).optString("endpoint").ifEmpty { null }` → 旧门仍绿。后果：符号模式 `"chat"` 被写进 `absoluteEndpointOverride`，请求 URL 拼接损坏（1019 ISSUE-01 复现）。

被挡住（判据承重，未动）：C2（dataflow 轴，见 selftest 的「非流式路径不解析 / 传常量」两臂）。

## 4. 修法

- **C2 → 值必须活着**：新增 `_post_process_override(expr)`：对 `let` / `run` / `takeIf` 这类**返回值**组合子取块体，块体里出现硬编码 `ThinkingLevel.<X>` ⇒ 覆盖；块体不引用其接收者（`it` 或显式 lambda 参数）⇒ 覆盖；块体无法配平 ⇒ fail-closed 覆盖。`_traces_to_level` 的直传分支与绑定分支都先过这道闸。
- **Rule 7 → 绑定必须停在富化调用**：新增 `_call_end` + `_alias_discards`：别名行的富化调用配平后，尾串只能是闭合定界符或纯成员读（`^[)\]}]*(?:\.\w+)*$`）；出现 `.let{` / `?:` / 再调用 ⇒ 红，报文点明「binds a DERIVED value」。别名仍留在集合内（下游 `model = alias` 不重复报警，缺陷定位在绑定行）。
- **Rule 1 → 按函数体认定**：新增 `_decl_body`（块体 / 表达式体，止于空行或下一条声明）+ `is_delegate_def` 重写：体必须是**恰好一次** `ModelUseProtocol.<fn>(` 调用，前缀只允许空或 `return`，尾串只允许闭合定界符或纯成员读（`.body` 这种合法）。掏空/追加回退一律红。

新增 selftest 臂 3 条：`an alias bound to a derived value (.let { entry.model })`、`a delegate with an elvis fallback must be caught`、`the parsed level is replaced after the read (.let { ThinkingLevel.OFF })`（后者用 `wiring_arm` 的「恰一条违规」断言）。

## 5. 四层证据

- L1 真树 `rc=0`；`--selftest` → `MODELUSE_PROTO_SELFTEST=PASS` + `MODELUSE_PROVIDER_MATCHER_SELFTEST=PASS`（收尾标记原样，`test_scan.py` 全等断言对象未改）。
- L2 反攻：3/3 CAUGHT（rc 0→1，restore sha256 一致）。
- L3 邻门：`shizuku_args_check`、`mcp_oauth_lifecycle_check` 各 rc=0。
- L4 全量：`scan.sh` 112 passed / 0 failed；`test_scan.py` 344 例 0 失败（分批）。

## 6. CI run

分支 `fix/meta-wide-07-mcp-tools`：run `<id>`（回填见最终回报），head_sha 与本地逐一核对。

## 7. 残留

- **`_post_process_override` 只覆盖 `let`/`run`/`takeIf`**：`?.also { }` 不返回值（不该拦），但 `when` / `if` 表达式或自定义扩展函数做等价覆盖不会被抓。触发条件：真树或新见证出现该类形态。触发后方案：把「块体不引用接收者 + 出现硬编码等级」的判定推广到任意尾随 lambda。搁置理由：当前三形态覆盖了 1046/wide-1024 两个实证，扩面会先红在 `.also` 之类无害写法上。
- **Rule 7 对 `.copy(...)` 类后续调用判红**：`effectiveModel(entry).copy(x = 1)` 会被当成派生值。触发条件：真树出现该形态。触发后方案：白名单允许「不改变富化字段」的成员调用（需能证明字段不变）。搁置理由：静态上无法证明 `.copy` 保留全部能力字段，严格侧更安全（红而非静默放行）。
- **Rule 1 只认 `ModelUseProtocol.` 前缀的委托**：`import … as Proto; Proto.parse…` 会红。触发条件：真树改用 import 别名。触发后方案：把 PREFIX 换成「文件顶部 import 派生出的别名集合」。搁置理由：现树统一全限定名。
- 已知测试串行化限制见 §8（`test_scan.py` 在本机需要分批跑，属沙箱 waitpid ENOSYS，非本支改动）。

## 8. 复现

- 反攻：`python3 /var/minis/shared/work/meta-wide/ops-07/reattack.py`（该门 3 条见证）。
- 单门：`python3 scripts/scan/modeluse_proto_single_source_check.py .` ／ `--selftest`。
- 脚本：`ops-07/patch_modeluse.py`、`ops-07/freeze_modeluse_arms.py`。
- 全量夹具分批：`nice python3 ops-07/run_part.py . <test_fn> …`（本机 `subprocess.wait` 偶发 `OSError 38`，需逐函数串行）。
