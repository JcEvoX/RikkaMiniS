# file_uri_parse_guard_check — file:// / minis:// 路径解析守卫（wide-1024）

> wide-1024 门见证加固 · 分支 `fix/meta-wide-08-file-preview` · 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
> 门文件 `scripts/scan/file_uri_parse_guard_check.py` · sha256 前 16 位 `44a90c9776370fa5`
> 本波**零生产代码改动**：五个 `.kt` 生产文件首字节未变（见 §5 L2 的 restore 记录）。

## 1. 门

`file_uri_parse_guard_check.py` — 钉住「`file://` 与 `minis://` URL 解码成宿主路径」的三处语义：解码结果必须原样使用；`#` 只在 http(s) 分支切；`minis:///var/minis/x` 解出的绝对路径不得再拼前缀。

自证标记（冻结面 `test_scan.py` 全等断言，**一字未改**）：`FILE_URI_PARSE_GUARD_SELFTEST=PASS`

## 2. 轴

**`dataflow`（F1a）+ `spelling`→决定点（F5c / F5d）**：

- F1a：调用了 `decodeUriPath(...)` ≠ 结果没被再切一刀。
- F5c：附近 400 字符里有 `startsWith("http` ≠ 那个测试决定了这次切分。
- F5d：同一行里有 `startsWith("/")` ≠ 那个条件是活的。

## 3. 见证（修复前 rc=0 / 修复后 rc≠0）

| 见证 | 旧判据为何看不见 | 新判据凭什么抓住 |
|---|---|---|
| F1a `decodeUriPath(url.removePrefix("file://")).substringBefore('#').substringBefore('?')` | 只查分支切片里**含**固定解析器 + 计数 | 解析编码调用的右括号，其后若接字符串改写链（`substringBefore/substringAfter/substring/replace/split/trim±/removePrefix/removeSuffix`）判红——「解完再切」等于把 `#`/`?` 截断原样加回来 |
| F5c `if (true \|\| noQuery.startsWith("http://") …)` | 只看切点前 400 字符里有没有 `startsWith("http` | 回溯到**包住该切点的最近一个 `if (…)`**，取其条件：条件里必须有 http(s) 测试；条件里出现 `true \|\|` / `false &&` 判死红；切点不在任何 if 里也红（fail-closed） |
| F5d `if (false && decoded.startsWith("/")) decoded else "/var/minis/$decoded"` | 只查该行里含 `startsWith("/")` | 解析该行自己的 `if (…)` 条件（括号感知）：条件必须是活的（无字面量短路）且真的测 `startsWith("/")`；读不出条件 → 红 |

## 4. 修法

1. 新增 `_match_paren()` / `post_decode_poison()`：按「调用的右括号之后」判定值是否被改写，F1a 与 F2a 共用同一判据函数（同族第二次 → 修在共享层）。
2. 新增 `last_if_cond(text, idx)`：找包住切点的最近一个 `if (…)` 条件，替代 400 字符窗口。F5c 改为「决定条件判定」。
3. F5d 改成解析行内条件：`DEAD_GUARD_RE`（字面量短路）+ `startsWith("/")` 双判据；条件不可解析 → fail-closed 红。
4. 改写链白名单只收**字符串改写**类（`substringBefore` 等）；`takeIf/let` 这类值转发不列入——真树 `StreamingMarkdownText.kt:1787` 附近合法地写了 `.takeIf { it.isNotBlank() }?.let { File(it) }`，把 lambda 体纳入判定会误伤，见 §7 R1。

设计约束（本波五门共用）：
- **解析失败 fail-closed**：括号/花括号失配一律报违规，消息里点明 `fail-closed`，绝不静默返回绿。
- **锚决定点，不锚拼写**：判据读的是「谁决定了这个值/这条控制流」，不是「文件里有没有这个词」。
- **不误伤合法写法**：真树必须仍绿；只把判据放宽到能过真树、同时仍抓住见证的那一档。

## 5. 四层证据

### L1 本门

    python3 scripts/scan/file_uri_parse_guard_check.py .        -> rc=0（真树绿）
    python3 scripts/scan/file_uri_parse_guard_check.py --selftest -> FILE_URI_PARSE_GUARD_SELFTEST=PASS

新增/改动臂（fixture 断言「恰好红对应判据」）：

    mut11 F1a 解完再切 (#/?)        （新）→ F1a 红，F1b 保持绿（精度断言）
    mut12 F5c `true \|\|` 短路守卫    （新）→ F5c 红，F5a 保持绿
    mut13 F5d `false &&` 短路守卫     （新）→ F5d 红
    原有 12 条（sheet/md/holder/allowlist/host-precision/media 家族）全部保持原判

### L2 见证反攻（唯一有效证据）

`probe/reattack_wide08.py`：对每条见证「打补丁 → 跑本门 → 必须 rc≠0 → 恢复 → sha256 比对」。

    F1a decoded path re-cut after decode        rc=1 restore=True
F5c http(s) guard short-circuited            rc=1 restore=True
F5d absolute-path guard short-circuited      rc=1 restore=True

判据链：同一补丁在修复前 rc=0（门瞎）→ 修复后 rc≠0（门抓住）⇒ **当初打穿它的那把刀现在被挡住**。
15 条见证 **15/15 CAUGHT**，15 次 restore 全部 `sha256` 一致（`git status --porcelain` 只剩五个门文件）。

### L3 邻门

    file_provider_paths_coverage_check      rc=0
    offload_cli_args_check                  rc=0
    offload_host_path_check                 rc=0
    offload_media_b64_wire_check            rc=0
    offload_relative_path_unify_check       rc=0
    skill_file_save_result_check            rc=0

### L4 全量

    bash scripts/scan/scan.sh      -> Summary: 112 passed, 0 failed
    python3 scripts/scan/test_scan.py -> ALL 344 CASES PASS

## 6. CI

分支 `fix/meta-wide-08-file-preview`，commit 见本文件所在提交；push 后手动 dispatch（`on: push` 只绑 main）。

- run id：`<见最终回报 / CI>`，`conclusion=<success>`，`head_sha` 与本支 HEAD 逐字符一致。
- 日志判读：`Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；含 `FAILED` 的行须逐条确认是 PASSED 用例名子串。


## 7. 残留（每条带三件）

- **R1（P2 登记）** 解码结果接 `.let { x.substringBefore('#') }` 这类「lambda 里再改写」不判红（`POST_DECODE_POISON_RE` 只看链上第一个成员）。
  触发条件：真出现 lambda 内改写，或审计再次打穿同一形态。
  触发后方案：把 `post_decode_poison` 扩成「取链尾表达式的字符串改写集合」，需做 lambda 体的轻量数据流（识别 `it` / 形参）。
  搁置理由：真树合法使用 `.takeIf{}?.let{}` 转发值，现在扩判据的误伤面（4 个 `"/var/minis/$decoded"` 站点 + markdown 渲染路径）大于收益；本条已由 mut11 覆盖「直接改写」这一实战形态。
- **R2（P2 登记）** F5c 的 `last_if_cond` 取「最近一个已闭合的 `if`」，若切点与守卫之间还有一层无关 `if`，会取错条件（偏严/偏松都可能）。
  触发条件：真出现嵌套 if 且切点在内层。
  触发后方案：改成建 AST 式嵌套栈（`if` 起止区间包含关系），而不是「最近一个」。
  搁置理由：`MarkdownBlockModel.mediaPathOfUrl` 只有一层 if，真树无嵌套。
- **R3（P2 登记）** F3（holder 分支：先切后解码再拼前缀）没有加「解完再切」判据，只保留了原有的前缀/占位符判据。
  触发条件：holder 路径改写形态变化。
  触发后方案：把 `post_decode_poison` 一并挂到 F3。
  搁置理由：本波见证只打穿 F1a；半径纪律——同一判据族里未被击穿的站点先登记，等触发。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-08-file-preview
    python3 scripts/scan/file_uri_parse_guard_check.py .
    python3 scripts/scan/file_uri_parse_guard_check.py --selftest
    python3 probe/reattack_wide08.py        # 15 条见证全量反攻（本支五门）
    bash scripts/scan/scan.sh               # 112 passed, 0 failed
    python3 scripts/scan/test_scan.py       # ALL 344 CASES PASS

见证夹具本体就在门文件里（`BAD_*` / `BREAKER_*` 常量），把对应 `find` 串换成 `replace` 串即可在真树上重放同一条攻击。
