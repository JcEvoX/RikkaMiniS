# FIX-META：`log_url_redaction_guard.py` — spelling 轴（判据只认一种求值拼写）

- 分支：`fix/meta-audit-m3-spelling`
- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- 门：`scripts/scan/log_url_redaction_guard.py`（1 文件，**生产代码 0 行改动**，新增门 0 道）
- 轴：`spelling` —— 判据匹配的是「url 被写进日志的那一种字符串拼写」，而缺陷本身是「url 的值被交给日志」

## 1. 这改变什么用户行为 / 防什么假绿

> **防的假绿**：把整条带 `?key=` 的 URL 用**字符串拼接**（`"...=" + forBaseURL`）或**花括号嵌套模板**（`${url.let { it }}`）送进日志时，门仍报绿——因为 `_offenders` 只跑一条 `DYN` 正则（`\$\{([^{}]*)\}|\$ident`），拼接根本不产生 `$`，而 `[^{}]*` 在第一个 `}` 就停，嵌套形态连匹配都不成立。
>
> **改后**：门判的是「日志语句里有没有出现**整个 url 值**」，覆盖三种求值形态（插值 / 拼接 / 嵌套包装）；解析不出时**保守报红**而不是静默放过。

## 2. 见证（外部审计给出，已被本门复核）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/provider/ModelsDevApi.kt",
 "find":"        // Phase 1: Exact API base match (with/without /v1)\n        val candidates = normalizedCandidates(forBaseURL)",
 "replace":"        // Phase 1: Exact API base match (with/without /v1)\n        com.rikkaminis.app.logging.AppLogger.info(TAG, \"fetchModels forBaseURL=\" + forBaseURL)\n        val candidates = normalizedCandidates(forBaseURL)",
 "why_gate_green":"_offenders 只看 DYN 正则的字符串模板插值；拼接或嵌套花括号不产生 DYN 匹配 ⇒ 0 违规。",
 "why_defect_back":"AppLogger.info 生产默认开启、无条件镜像 logcat + filesDir/logs。带 ?key= 的 base URL 整串明文进持久化日志。"}
```

- 修前复证：`verify_witness.py report-A.md` → `rc=0 · GATE_BLIND_CONFIRMED`（门瞎实锤）
- 修后复证：同命令 → **`rc=1 · GATE_CAUGHT_WITNESS · restore=True`**，命中行精确：
  `src/android/app/src/main/java/com/rikkaminis/app/provider/ModelsDevApi.kt:98`，`泄漏形态: concat(after): forBaseURL`

## 3. 现状判据 → 新判据

| | 修前 | 修后 |
|---|---|---|
| 扫描单位 | `LOG_CALL` 匹配行 + 12 行内括号配平 span | 同（span 语义不变） |
| 形态覆盖 | 仅 `$ident` / `${expr}`（`[^{}]*`，**遇 `{` 即断**） | ①插值（brace 平衡扫描，见下）②拼接 ③嵌套包装 |
| 拼接 | 完全不可见 | `CONCAT_AFTER` / `CONCAT_BEFORE`：`+` 两侧都读、允许跨行（Kotlin 长参数换行），操作数是成员/调用**链**（`call.request().url` 也抓到） |
| 嵌套 | `[^{}]*` 结构上匹配不到 | `_scan_braced()` 按**花括号配平**取整条 `${...}`，`.let { it }` 等恒等包装不再豁免（`SAFE_TAIL` / `REDACTOR` 白名单照旧） |
| fail-closed | 未闭合语句按截断 span 分析（漏） | 语句 12 行内未闭合、或 `${` 无匹配 `}` ⇒ **报红**，不静默放过 |
| 误报控制 | `EXEMPT`(`url-ok:`) / `NARROW_NAME` / `REDACTOR` / `ALLOWED` | 全部保留；另加 `NOT_A_URL_VALUE`（`urlCount`/`urlLen` 这类**计数**而非载体，避免第一条 `"urls=" + urlCount` 就变噪音） |
| 字面量剥离 | 无 | 拼接判据跑在「字面量内容抹空」的文本上——`+` 是字面量**之外**的运算符，格式串里的 `"+ url"` 不应被当成运算 |

关键实现点（都可被读到代码的人复核）：

- `_strip_string_literals()`：把每个字符串字面量内容换成 `""`，保留引号位置；拼接正则只跑在这份文本上。
- `_scan_braced()`：从 `${` 起做 `{`/`}` 配平；返回 `(text, next_index)`，`text=None` 表示未闭合 → 调用点报 `"${…} (unterminated)"`。
- `_is_url_operand()`：双闸（名字含 url **且** 不是 narrow 分量 / 不是计数名 / 不含 redactor）才判命中。
- `_strip_span()` 增加第三返回值 `balanced`；`scan()` 对 `balanced=False` 直接产出 finding。

## 4. 见证固化为 selftest 臂

`--selftest`（兼容旧写法 `--self-test`）从「一个 fixture 断言两个行号」重写为 **17 条命名臂**，每条一个独立目录、独立断言，输出 `URL_REDACTION_SELFTEST=PASS (17 arms)`：

| 类别 | 臂 | 期望 |
|---|---|---|
| 插值（原覆盖） | `interp-whole-url` / `bare-name-leaks` | RED |
| 插值白名单 | `interp-redacted` / `safe-accessors` / `inline-exempt` / `non-log-statement` | GREEN |
| **见证①拼接** | `concat-after`（**见证原文逐字**）、`concat-after-url-object`、`concat-before`、`concat-multiline` | RED |
| 拼接白名单 | `concat-redacted` / `concat-safe-component` / `concat-count` | GREEN |
| **见证②嵌套** | `nested-brace-identity`（`.let { it }`）、`nested-brace-plain` | RED |
| 嵌套白名单 | `nested-brace-redacted` | GREEN |
| fail-closed | `unbalanced-statement` | RED |

两条见证臂是**原样**写进去的：外部审计用它们把这道门打穿，它们就该是这道门最不会忘记的两个形状。

## 5. 四层证据

- **L1（本门）**
  - 真树：`✅ log url guard: no whole url value reaches a log — judged across all evaluation forms (interpolation / concatenation / brace-nested) (audited sites: 13)`，`rc=0`
  - selftest：`URL_REDACTION_SELFTEST=PASS (17 arms)`，`rc=0`
  - **真树未出现新命中**（预期 rc=0 成立）：改动前先做了两轮真树形态探测（扫描目录 127 文件 / 419 条日志语句），拼接形态 url 命中 **0**、嵌套花括号 url 命中 **0**、日志 span 内 char 字面量 **0** ⇒ 新判据不触碰现有任何站点，`ALLOWED` 一条未加。
- **L2（机械复证，必过）**：见 §2，`log_url_redaction_guard` → `GATE_CAUGHT_WITNESS`，`restore=True`（补丁回滚后 sha 校验一致）。
- **L3（全量）**：`sh scripts/scan/scan.sh` → **`Summary: 112 passed, 0 failed`**（114 个门头 `━━━`，exit 0）；`python3 scripts/scan/test_scan.py` → **`ALL 344 CASES PASS`**（345 个 ✅、0 个 ❌/FAIL）。
- **L4（CI）**：见 §6。

## 6. CI

- run：`（回填见下一条提交）`
- 判读项：`head_sha` 与本地 HEAD 逐字符一致 · `Summary: 112 passed, 0 failed` 恰一条 · `ALL 344 CASES PASS` 恰一条 · `skipped` 仅 Publish（main 门控）。

## 7. 残留登记（本支不动手）

| # | 残留 | 判级 | 触发条件 |
|---|---|---|---|
| R1 | `ALLOWED` 里 13 条「已审计站点」是**行级白名单**（`(rel, snippet[:70])`）：同一行改一个字就失配、白名单自动失效（偏安全方向）；但同一行**追加**一次拼接仍会失配 → 变成红而非静默放过，属可接受方向 | P3 | 若将来出现「白名单行被改写导致 CI 假红」再考虑改哨兵注释 |
| R2 | 拼接判据只覆盖 `+`；Kotlin 的 `StringBuilder().append(url)` / `buildString { append(url) }` 仍不可见 | P2 | 出现首个此类站点时扩等价类（形态同 `_scan_braced`） |
| R3 | `nested-brace` 判据只看 URL 名字；`"${someLambda { url }}"` 这类**间接**引用（lambda 体内取 url）依赖名字出现在同一表达式内才可见 | P2 | 首个类似误漏出现时扩数据流 |
| R4 | 本门的 `--selftest` **未进 `test_scan.py`**（冻结面）：CI 每次只跑真树分支，17 条臂只在本地/审阅时跑 | P1 | 并集阶段由主会话统一注册（新门/新夹具登记是收口动作，不是支线动作） |
| R5 | `_strip_span` 的字符串切换只看 `"`，不看 `'`（Kotlin char 字面量）。真树 0 例（本轮探测 419 条语句）；若出现含 `'.'` 的字符字面量会被误认成窄名而跳过 | P2 | 首次出现 span 内 char 字面量 |

## 8. 复现命令

```sh
cd /var/minis/shared/work/meta-audit/wt-m3

# L1 本门
python3 scripts/scan/log_url_redaction_guard.py .
python3 scripts/scan/log_url_redaction_guard.py --selftest

# L2 见证复证（必须 GATE_CAUGHT_WITNESS）
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md \
  --repo /var/minis/shared/work/meta-audit/wt-m3

# L3 全量
sh scripts/scan/scan.sh
python3 scripts/scan/test_scan.py
```
