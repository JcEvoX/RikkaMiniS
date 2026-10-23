# dv25-FIX-2 · A-02 + A-03 + A-04（P1×3）provider 流契约三连

> 本报告由主会话收口时按 `backlog §1045-A02/A03/A04` + 分支提交记录 + CI 判读重建（支 2 会话未落盘报告文件）。

## 1 身份链

- 基线 main `1d44ec90`；分支 `fix/dv25-a2-provider-stream-contract`，3 提交（`c912757d` 生产 / `8f745bef` 测试 / `9dffdf1a` 门）
- 9 文件 **+768/−32**；门 109 / 夹具 336 不变（0 新建门；`truncated_call_guard_check.py` 扩 C6/C7、`tool_result_image_branch_check.py` 扩空消息过滤判据）
- CI run `39917668200`（#2549）success，`head_sha=9dffdf1a` 与本地/远端逐字符一致（EXACT）

## 2 缺陷本体

三条同族：**边界已经存在，某个入口没接 / 被架空**。

- **A-02** `GeminiProvider` SSE EOF 的 `else` 臂合成 `lastFinishReason ?: "end_turn"`，而 `failOnSilentEmptyCompletion` 以 `Finished.stopReason != null` 判「干净结束」⇒ 真空 EOF（上游丢 SSE body）被伪装成合法 end_turn，不抛 `TransientError`、不重试；同故障在 OpenAI / Anthropic 上会报错重试。
- **A-03** 三处空消息过滤（`OpenAIProvider` ×2 / `AnthropicProvider` ×1）只问「有没有文本」，而纯图片 / 纯音频轮 `content` 恒空 ⇒ 整条 user 轮被滤掉，模型从未看到字节（下游三个「附加媒体」分支因此不可达）。
- **A-04** `AnthropicProvider` 流内 `error` 事件只把 `overloaded_error` 映 529、其余一律 400 ⇒ `rate_limit_error` 落 `mapHttpError` fallthrough = 不可重试的 `ProviderError`，而同条件的 HTTP 429 是可重试 `RateLimited`。

## 3 修法（阶梯第 6/7 级）

- A-02：新增纯函数 `geminiEofTerminal(...)`，无 finishReason 一律**原样传 null**；`shouldFlagEofTruncated` 公开语义一字未动（任务书禁区）。
- A-03：新增单源 `provider/MessagePayloadFilter.kt:hasWirePayload(...)`（文本 / contentParts / audioParts / 消息级 imageParts / **最后一轮 user 的顶层图片**），三处改走它；顺带把「无文本也发空 text 块」两处改为无文本不发。
- A-04：新增纯函数 `anthropicStreamErrorStatus(...)`，按 Anthropic 错误类型表映射（`overloaded_error→529` 逐字保住、`api_error→500`、`rate_limit_error→429`、`authentication_error→401`、`permission_error→403`、`not_found_error→404`、`request_too_large→413`、`invalid_request_error→400`、未知/空→400）；`mapHttpError` 本体未动。

## 4 门（两道既有门扩判据，0 新建门）

- `truncated_call_guard_check`：C6（EOF 终局必须走纯函数 + 规定发射形状）/ C7（**先 strip 注释再扫** —— 修复 KDoc 里逐字引用了缺陷旧行；else 臂必须原样透传且文件内不得出现 `lastFinishReason ?:`）。2 带牙臂。
- `tool_result_image_branch_check`：空消息过滤站点必须走共享单源 + 单源必须同时权衡 audioParts / imageParts / 顶层轮图片 + 单源文件不得消失；**站点从真树派生**（`.filter`/`.filterIndexed` 且体含载荷判定，两种形态都认）。4 带牙臂 + 1 绿臂。

## 5 验证四层

- L1 本门真树 rc=0 ×2 + `--selftest` 13 臂全绿 + `scan.sh` **109/0** + `test_scan` **ALL 336**
- L2 概念反查邻门 3/3 rc=0（`provider_ua_parity_check`——其 C 判据直接锚 `shouldFlagEofTruncated(`；`thinking_wiring_guard`；`gemini_base_version_check`）
- L3 真 kotlinc 2.0.21 + 真 JUnitCore 八臂 ALL MATCH：POST `OK (23 tests)` / PRE-gemini 精确 2 红 / PRE-anth 精确 6 红 / 5 变异臂各精确红；**PRE 臂取 `git show 1d44ec90:` 基线 blob 并对基线文本做字面存在性断言**（取不到即退出）；变异只施加在提取副本上
- L4 CI #2549：26 步唯一 skipped=Publish、`109 passed, 0 failed`、`ALL 336`、门头 1..109 互异、`Z e: ` 0、游离 FAILED 0、新增 23 条用例逐条 PASSED

## 6 报告失真 / 任务书偏离

任务书 FIX-2 §2/§3 对 A-03 的修法只描述消息级字段、补不上顶层 `imageParts`，且「三处 filter 行必须含 audioParts 与 imageParts」会把**正确的单源化修复判成红**。实际按「站点必须走单源 + 单源必须权衡哪几支」实现。

## 7 三条规则级收获

1. **KDoc 里的「交给 X 处理」必须能在代码里指出「X 凭什么会接手」的那一行**，否则是伪承诺（Gemini 的 KDoc 与同一段的 `?: "end_turn"` 正是此例）。
2. **过滤器谓词必须回答过滤器真正要问的问题**：「文本/媒体/工具」是多维的，凭据维度不全 ⇒ 载荷被静默删除（请求照样 200）。
3. **解除一条过滤 = 打开一批原先不可达的分支**，那些分支的形状必须逐个复核（媒体轮活下来后「无文本也发空 text 块」从不可达变成可达，直接成为新 400 源）。

## 8 残留

R1 真机行为级（真空 EOF 报错重试 / 纯媒体轮对端可见 / 限流提示可重试）· R2 `geminiEofTerminal` truncated 分支不带 stop reason 是刻意二分（P3）· R3 「最后一轮 user」依赖顶层 `imageParts` 契约（P2）· R4 「无文本不发空 text 块」只改到本支触达两处（P2，半径纪律）· R5 Anthropic 映射表未知 type 落 400（P2）· R6 `MessagePayloadFilter.kt` 不在任何 `PAYLOAD_FILES` 白名单（P3）· R7 两个 EOF 函数判定表分散、刻意不合并（P3，禁区）· R8 任务书措辞失真（已登记）· R9 `probe/l3/` 未入库（批次惯例）。全带触发条件，见台账。
