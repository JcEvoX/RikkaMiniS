# file_preview_url_encode_check — 文件预览 file:// URL 编码（wide-1024）

> wide-1024 门见证加固 · 分支 `fix/meta-wide-08-file-preview` · 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
> 门文件 `scripts/scan/file_preview_url_encode_check.py` · sha256 前 16 位 `88c1000d57907a59`
> 本波**零生产代码改动**：五个 `.kt` 生产文件首字节未变（见 §5 L2 的 restore 记录）。

## 1. 门

`file_preview_url_encode_check.py` — 钉住 `ui/sandbox/FilePreviewScreen.kt` 的 file:// URL 构造契约：原始路径不得拼进 URL，编码器必须真的编码，`fileUrlFor` 必须把编码器的值交出去，两个 load 点必须原样消费。

自证标记（冻结面 `test_scan.py` 全等断言，**一字未改**）：`FILE_PREVIEW_URL_ENCODE_SELFTEST=PASS`

## 2. 轴

**`anchor` + `dataflow`**：旧判据三条全是「某物存在」（声明头存在、前缀子串存在、调用点计数为 2），因此三种改写都能过：编码器体被短路、编码结果被 `.let` 丢掉、load 点把 URL 换掉。

## 3. 见证（修复前 rc=0 / 修复后 rc≠0）

| 见证 | 旧判据为何看不见 | 新判据凭什么抓住 |
|---|---|---|
| 编码器体首行 `if (true) return path` | arm2 只匹配 `internal fun encodeFileUrlPath(` 声明头 | 解析函数体（花括号体 / 表达式体两种形态）：必须出现百分号转义发射；**任何 `return <参数名>` 判红**；构造了转义缓冲（`sb.append('%')` 等）却没把它 return 出来也判红 |
| `fileUrlFor` → `"file://" + encodeFileUrlPath(file.absolutePath).let { file.path }` | arm2 查的是 `'"file://" + encodeFileUrlPath('` 这个前缀在不在 | 解析 `fileUrlFor` 体：`"file://" +` 恰一处、`encodeFileUrlPath(` 恰一处；**编码调用的右括号之后**若是 `.let/.run/.also/.apply/.takeIf/.takeUnless` 或 `?:` 判红；块体形态必须 `return … encodeFileUrlPath(…)` |
| load 点 → `fileUrlFor(item.file).let { "file://" + item.file.path }` | arm3 只数 `fileUrlFor(item.file)` 出现 2 次——计数在改写后仍是 2 | 每个 load 点的调用右括号之后不得接值丢弃链；另把 `"file://" +` 的**操作数**纳入判据：不是 `encodeFileUrlPath(` 开头即红（这条同时覆盖 load 点里的裸拼） |

## 4. 修法

1. 新增解析工具 `_match()` / `_block_at()` / `_fun_parts()`（区分花括号体与表达式体，表达式体按「行尾操作符续行」聚合），`check_encoder()` / `check_file_url_for()` 全部改成**读体**而不是读头。
2. `check_encoder()`：转义发射（`'%'` append / `%02X` / `Uri.encode` / `URLEncoder.encode`）+ 转义载体（被 append 了 `'%'` 的变量）必须出现在某个 return 表达式里 + 不得 `return <参数名>`。
3. `check_file_url_for()` + `check_load_sites()`：把「值被改写」显式化（`VALUE_DISCARD_RE` / `ELVIS_RE`），并对 `"file://" +` 的操作数做形态判定。
4. 合规夹具重写为**真编码器**（逐字节 `%XX`、返回 `sb.toString()`；含 `#` / 空格 / 中文名的原注释场景），否则旧夹具本身就会被新判据正确判红。

设计约束（本波五门共用）：
- **解析失败 fail-closed**：括号/花括号失配一律报违规，消息里点明 `fail-closed`，绝不静默返回绿。
- **锚决定点，不锚拼写**：判据读的是「谁决定了这个值/这条控制流」，不是「文件里有没有这个词」。
- **不误伤合法写法**：真树必须仍绿；只把判据放宽到能过真树、同时仍抓住见证的那一档。

## 5. 四层证据

### L1 本门

    python3 scripts/scan/file_preview_url_encode_check.py .        -> rc=0（真树绿）
    python3 scripts/scan/file_preview_url_encode_check.py --selftest -> FILE_PREVIEW_URL_ENCODE_SELFTEST=PASS

新增/改动臂（fixture 断言「恰好红对应判据」）：

    compliant (#/space/CJK through the encoder) （重写）真编码器 + 两个 load 点        → 绿
    violation-bare-template                      （旧）`file://${item.file.absolutePath}`  → 红
    violation-no-encoder                         （旧）`"file://" + file.absolutePath`     → 红
    violation-encoder-short-circuit              （新）体内 `if (true) return path`        → 红 "returns its input unchanged"
    violation-encoder-hollow                     （新）体掏空成 `= path`                   → 红 "never emits a percent escape"
    violation-fileurlfor-discard                  （新）`.let { file.path }`                → 红 "discards the encoder's value"
    violation-load-site-discard                   （新）load 点 `.let { "file://" + … }`    → 红 "rewrites fileUrlFor(item.file)"
    每条负向臂都断言「命中的是那条判据」（`expect_msg` 子串），不是「有任何违规就算过」

### L2 见证反攻（唯一有效证据）

`probe/reattack_wide08.py`：对每条见证「打补丁 → 跑本门 → 必须 rc≠0 → 恢复 → sha256 比对」。

    encoder body short-circuited                 rc=1 restore=True
fileUrlFor discards the encoder value        rc=1 restore=True
load site rewrites the encoded URL           rc=1 restore=True

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

- **R1（P2 登记）** 编码器若改成 `Uri.encode(path, "/")` 之类标准库形态：`ESCAPE_EMIT_RE` 认 `Uri.encode`，但 `ESCAPE_CARRIER_RE` 无载体时会走到「返回表达式必须调 Uri.encode」分支——已覆盖；不过 `Uri.encode` 的**第二个参数**（允许保留的字符集）不做语义判定（`Uri.encode(x, "")` 与 `Uri.encode(x, "#")` 同等对待）。
  触发条件：真出现带 allowlist 参数的写法。
  触发后方案：加「allowlist 不得含 `#`/`?`/`%`」判据。
  搁置理由：真树手写编码器，零使用；现在写等于猜 API。
- **R2（P2 登记）** `fileUrlFor` 若被包一层（`fun previewUrl(item) = fileUrlFor(item.file)`）后再传给 `loadUrl`，新 load 点不在 `LOAD_SITE_RE` 的计数里（计数会变成 1 → 红，属 fail-closed，不会静默放过）。
  触发条件：出现包装函数 → 门红。
  触发后方案：把 load 点判据改成「从 `loadUrl(` 实参向上回溯到编码器」，不再数固定 token。
  搁置理由：当前两个 load 点是直传，包装层不存在。
- **R3（P2 登记）** 编码器的「载体」判定依赖 `sb.append('%')` 这种显式形态；若改用 `String.format("%%%02X", v)` 逐段拼接，载体集合为空，会落到「返回表达式必须调 Uri.encode」分支 → 误红。
  触发条件：编码器改写形态 → 门红（人工复核一次即可）。
  触发后方案：`ESCAPE_EMIT_RE` 增加 `String.format("%%%02X"` 形态，并把 `exprs` 里的 `buildString {}` 视为载体。
  搁置理由：真树形态固定，扩形态等于给未来的改写留洞。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-08-file-preview
    python3 scripts/scan/file_preview_url_encode_check.py .
    python3 scripts/scan/file_preview_url_encode_check.py --selftest
    python3 probe/reattack_wide08.py        # 15 条见证全量反攻（本支五门）
    bash scripts/scan/scan.sh               # 112 passed, 0 failed
    python3 scripts/scan/test_scan.py       # ALL 344 CASES PASS

见证夹具本体就在门文件里（`BAD_*` / `BREAKER_*` 常量），把对应 `find` 串换成 `replace` 串即可在真树上重放同一条攻击。
