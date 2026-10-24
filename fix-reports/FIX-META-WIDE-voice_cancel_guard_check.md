# FIX-META-WIDE-voice_cancel_guard_check

- 分支：`fix/meta-wide-06-turn-loop-speech`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：wide-1024 组 A（`Report_A.md`）· 3 条门瞎实锤
- 结论：3/3 见证由 rc=0 翻成 rc≠0，真树仍绿，生产代码零改动

## §1 门

`scripts/scan/voice_cancel_guard_check.py` — 守语音链路上的「取消语义不被吞掉」：

| 判据 | 守什么 |
|---|---|
| A | `VoiceProvider` 里取消钩子（`call.cancel()`）必须在**活的**控制流上——被 `if (false)` / 永假条件包住的 cancel 等于没有 |
| B | `parseChatAsrTranscript(…)` 的返回值必须**被消费**进 `text`（直接或经局部绑定），且不许用硬编码空串兜底 |
| D | `OpenRouterModelsApi` 的 `CancellationException` 分支必须真重抛（`throw e` 在活路径上），且不许在 catch 里 `return@withContext emptyList()` ——否则取消会伪装成「没有模型」 |

## §2 轴

| 见证 | 判据 | 轴 | 备注 |
|---|---|---|---|
| W1 | A `dead_guard_cancel_hook` | dataflow | cancel 调用存在但结果不被执行 |
| W2 | B `parse_result_consumed` | dataflow | 解析结果被丢弃 + 空串兜底 |
| W3 | D `openrouter_cancellation_rethrow` | dataflow | 重抛在永假守卫后 + 空列表兜底 |

## §3 见证（补丁 → 旧门 → 新门）

| 见证 | 补丁要点 | 旧门 | 新门 | 新门读数 |
|---|---|---|---|---|
| W1 | 把 `call.cancel()` 包进 `if (false) { … }` | rc=0 | rc=1 | `VoiceProvider.kt: call.cancel() sits behind the dead guard …` |
| W2 | `parseChatAsrTranscript(...)` 调用后不接结果，改 `text = ""` | rc=0 | rc=1 | `VoiceProvider.kt: the parseChatAsrTranscript( … ) result is DISCARDED — it …` |
| W3 | catch 里 `if (false) throw e` + `return@withContext emptyList()` | rc=0 | rc=1 | `OpenRouterModelsApi.kt: `throw e` sits behind the dead guard …` + `… returns the empty fallback (return@withContext emptyList())` |

旧门瞎的共同形态：**只查「这个调用/这段文本出现过」**。
- W1：cancel 判据只要求函数体里出现 `.cancel(`，被永假条件包住照样命中；
- W2：只要求出现 `parseChatAsrTranscript(`，返回值去哪、有没有被空串顶掉不看；
- W3：只要求 catch 块里出现 `throw e`，永假守卫下的重抛也算数；`emptyList()` 兜底完全未判。

## §4 修法

1. **brace-block 解析 + 死条件判定（W1/W3）**：把 `invokeOnCompletion { … }` / `catch (…) { … }`
   的块体按花括号配平切出来，逐语句判「最近的封闭 `if (…)` 条件是否恒假」。
   恒假识别：字面 `false` / `!true` / 纯字面量比较（如 `1 == 2`）。
2. **消费链判定（W2）**：解析调用的结果必须是
   `text = parse(…)` 的直接赋值，或 `val x = parse(…)` 后 `text = x`（含 `trim()` 等薄包裹）；
   函数体内出现硬编码 `text = ""` 直接报违规（那正是「解析失败伪装成空转写」的形状）。
3. **取消兜底判定（W3）**：`CancellationException` 分支内 `return@withContext emptyList()`
   （或等价字面空列表）一律红，消息指向「取消被伪装成没有模型」。

## §5 四层证据

- **L1**：真树 `rc=0`；`--selftest` → **13 条 fixture**，逐条用 `expect_trip(fixture, 代码, 期望片段)`
  断言「该红的红、红的理由对」，收尾标记 `VOICE_CANCEL_GUARD_SELFTEST=PASS`。
- **L2**：3/3 `CAUGHT rc=1` + `restore=True`（见 §3）。
- **L3 邻门**：`speech_active_engine_check.py` rc=0、`mediaplayer_release_guard.py` rc=0。
- **L4**：`scan.sh` 112 passed / 0 failed；`test_scan.py` 46 个夹具函数逐进程 rc=0
  （本门走 4 元组带牙标记断言：`("voice_cancel_guard_check", "VOICE_CANCEL_GUARD_SELFTEST=PASS", (), (REPO_ROOT,))`，
  标记被掏空即红）；CI `ALL 344 CASES PASS`。

## §6 CI run

run `40137671947`（#2642）/ job `121002546481` / **success**（attempt 1）/
head_sha `98a65e7bf3df82da26feeee4d294a042c2a95f71` == 本地 HEAD / 26 步 `not_ok=[]`、
唯一 skipped = `Publish to Releases`（main 门控）/ `Summary: 112 passed, 0 failed` /
`ALL 344 CASES PASS` / ` e: ` 0 / 含 FAILED 的 6 行全是 PASSED 用例名子串。

## §7 残留（三条，各带三件）

**R1 · 死条件只识别字面常量**
- 触发条件：用间接常量写永假守卫（`private val NEVER = false` 然后 `if (NEVER)`），
  或 `if (BuildConfig.X)` 这类编译期常量。
- 触发后方案：把「常量折叠」扩到同文件 `private val` 字面量（值域求值），以及
  `if (neverCalled())` 形态的「函数体只 return false」一跳解析。
- 搁置理由：三个作用域文件当前无间接口子；此洞是 fail-open 的（只漏不误伤），
  且真正要防的形态（`if (false)` / `if (1 == 2)`）已覆盖。

**R2 · 消费链只认 `text = …` 与单层 `val` 绑定**
- 触发条件：`text = parse(…).trim()` 之外的正当写法——如 `text = runCatching { parse(…) }.getOrNull().orEmpty()`，
  或把结果先塞进 `VoiceInputResponse(text = …)` 再返回。
- 触发后方案：改成「解析调用的返回值必须出现在某个 `text =` 右值的数据流里」（一跳别名 + 括号感知），
  而不是枚举允许的形状。
- 搁置理由：真树是直线写法；误伤方向是报红（fail-closed），不是静默放行。

**R3 · D 判据锚全限定名 `kotlinx.coroutines.CancellationException`**
- 触发条件：改用短名 `catch (e: CancellationException)`（import 之后）。
- 触发后方案：同时接受短名，且要求文件里存在对应 import（或同包声明）。
- 搁置理由：真树用全限定名；短名时门报「CancellationException catch missing」= 显式失败。

## §8 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-06-turn-loop-speech
python3 scripts/scan/voice_cancel_guard_check.py .            # rc=0
python3 scripts/scan/voice_cancel_guard_check.py --selftest   # 13 fixtures, VOICE_CANCEL_GUARD_SELFTEST=PASS
python3 /var/minis/shared/work/meta-wide/w06_probe.py voice_cancel
```
