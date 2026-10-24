# FIX-META-parts-json-funnel — 元审计见证固化（axis=dataflow）

- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`（main）
- 分支：`fix/meta-audit-m5-scope-order`
- 门：`scripts/scan/parts_json_funnel_check.py`（只动 R11 / R12 两条判据）
- 轴：**dataflow**（数据流）—— 值有没有真的走进计数器，而不是分支上有没有那个名字
- 生产代码：**0 行改动**

## 1. 门在防什么

`ChatPartsAttachmentCount.countPartsAttachments()` 是导出统计的唯一计数面：它按 `parts_json` 条目区分「这是图片还是视频」并累加 `img` / `vid`，最终写进 `Summary.imageAttachments / videoAttachments` 与 `session.json`。当前版本的条目携带的是**现代序列名** `mediaRef`（顶层 `MediaPart` 与工具产出的 `toolResult.value.mediaRef` 两处），旧版才是 `image` / `image_url` / `video` / `video_url`。

R11 防「只认旧名字 ⇒ 对所有现存条目恒计 0」；R12 防「工具产出的媒体（截图、录像）挂在 toolResult 嵌套层，被顶层统计漏掉」。

门要防的假绿：**分支名字对，值一个没数**。

## 2. 见证（外部对抗审计给出，已机械复证）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/share/ChatPartsAttachmentCount.kt",
 "find":"            \"mediaRef\" -> {\n                if (isVideoMediaRef(value)) vid += 1 else img += 1\n            }\n            // …toolResult 注释…\n            \"toolResult\" -> {\n                val media = value?.optJSONObject(\"mediaRef\")\n                if (media != null) {\n                    if (isVideoMediaRef(media)) vid += 1 else img += 1\n                }\n            }",
 "replace":"            \"mediaRef\" -> Unit\n            // …toolResult 注释…\n            \"toolResult\" -> {\n                value?.optJSONObject(\"mediaRef\")\n            }"}
```

打上补丁：`"mediaRef"` 仍出现在带 `->` 的行上（R11 满意），`toolResult` 区间内仍含子串 `"mediaRef"`（R12 满意）—— **旧判据两条全绿，rc=0**。而实际上两个分支都不再递增任何计数器 ⇒ 导出统计对当前版本所有图片/视频/工具截图仍恒为 0（1047 A-04 重现）。

## 3. 旧判据 → 新判据

### R11（顶层 mediaRef 分支）

旧：`has_media_ref_dispatch_arm(body)` = 体内存在一行同时含 `"mediaRef"` 与 `->` 或 `==`。
→ `"mediaRef" -> Unit` 满足它。**判的是拼写，不是计数。**

新（`arm_accumulates`）：

1. `brace_balanced_arm()` 花括号感知地抽分支体（`"mediaRef" -> { … }` 取整块；`-> expr` 取该行），嵌套 `if {}` 不会把块边界切错。
2. 分支体必须**移动一个计数器**：`x += …` / `x = x + n` / `x++` / `++x`。
3. 但**允许委托**：分支体调用的**同文件**函数体内若有增量，算通过（一跳，与门里既有的 `expand_same_file` 同思路）—— 把计数搬进 helper 是正当重构，不该被判红。
4. 分类结果分三桶：`legacy_rows`（认不出 `mediaRef`）/ **`silent_rows`（认得出但不动计数器）** / `both_rows`。前两桶分别出独立的 FAIL 文案。
5. fail-closed：树的 `type` 分发点推导不出来 → 红（推导失明 ≠ 树里没有违规面）。

### R12（toolResult 嵌套 mediaRef）

旧：`tool_result_arm_reads_media_ref` = `"toolResult"` 行到下一个 `"mediaRef"` 行之间含子串 `mediaRef`。
→ 裸语句 `value?.optJSONObject("mediaRef")` 满足它（读是发生了，值被丢掉）。

新（`tool_result_arm_counts`）—— 两个半边都要：

1. **分支体抽得准**：改用同一个 `brace_balanced_arm`（旧的「到下一个 mediaRef 行」在 witness 形态下会把后面的兄弟分支一起吞进来，反而给假绿）。
2. **读出来的值被消费**（`nested_read_consumed`）三种形态：
   - 绑定并使用：`val media = …optJSONObject("mediaRef")` 之后 `media` 再被引用；
   - 直接传参：读表达式位于另一个调用的实参表内（括号平衡判定，`isVideoMediaRef(value?.optJSONObject("mediaRef"))`）；
   - 链式：`…optJSONObject("mediaRef")?.let { … }` / `?.also` / `?.run` / `?.takeIf`。
3. **并且移动计数器**（`arm_accumulates`，同 R11，含同文件一跳）。

fail-closed：分支体抽不出 → 红。

## 4. 见证固化（`--selftest` 新臂）

fixture 由 `MEDIA_GREEN` 经**带断言的两处替换**生成（`witness_shape()`），并在 selftest 里先断言产物确实 ≠ `MEDIA_GREEN`（`media-witness-binding` 臂）—— 否则锚点漂移会让新臂悄悄去判一个绿色 fixture，臂还是"通过"。

| 新臂 | 形态 | 期望 |
|---|---|---|
| `media-witness-binding` | 见证 fixture 真被替换（防锚点漂移静默退化为绿色） | OK |
| `r11-witness-arm-without-count` | `"mediaRef" -> Unit` | R11 红、句内点名该 face |
| `r12-witness-arm-discards-read` | `value?.optJSONObject("mediaRef")` 裸语句 | R12 红 |
| `r11-delegated-count-stays-green` | 计数搬进同文件 `countInto()/Counters` | R11 **绿**（不误伤重构） |
| `r12-delegated-count-stays-green` | 同上，toolResult 走传参 + helper 增量 | R12 **绿** |

```
SELFTEST r11-witness-arm-without-count: OK (gate RED: [R11])
SELFTEST r12-witness-arm-discards-read: OK (gate RED: [R12])
SELFTEST r11-delegated-count-stays-green: OK (PASS all 1 image/video parts_json faces classify "mediaRef" …)
SELFTEST r12-delegated-count-stays-green: OK (nested toolResult media counted)
PARTSJSON_FUNNEL_SELFTEST=PASS
```

既有 6 条 R11/R12 臂（pre-fix shape / second face / precision / dropped / outside-arm）期望全部未变。

## 5. 四层证据

| 层 | 内容 | 结果 |
|---|---|---|
| L1 | 真树 + `--selftest` | 真树 R11/R12 双 PASS；selftest **29 臂全 OK**（`PARTSJSON_FUNNEL_SELFTEST=PASS`） |
| L2 | `verify_witness.py report-B` | `parts_json_funnel_check` → **`GATE_CAUGHT_WITNESS`**、`restore=True`；见证打回真树后 R11 报 `1 image/video parts_json faces dispatch on "mediaRef" and MOVE NO COUNTER`、R12 报 `… read the nested mediaRef … and DISCARD it`，还原后双 PASS |
| L3 | 全量 | `scan.sh` → **`Summary: 112 passed, 0 failed`**（exit 0）；`test_scan.py` → **`ALL 344 CASES PASS`**（exit 0） |
| L4 | 分支 CI | run **#2622**（§6） |

L2 双向：修前 `rc=0 → GATE_BLIND_CONFIRMED`，修后 `rc=1 → GATE_CAUGHT_WITNESS`，两次 `restore=True`。

## 6. CI

- 分支 push 不触发（`paths` 只认 `src/android/**`、`src/shared/**`、`deps/**`、workflow 自身），手动 dispatch。
- run **#2622** / `head_sha = f64914d216bc80dc74263f36ba65ae1b525ba289`（与本地 HEAD 逐字符一致）
- 期望：`Summary: 112 passed, 0 failed` 恰一条、`ALL 344 CASES PASS` 恰一条、skip 只有 `Publish`、` e: ` 0。

## 7. 残留登记（不动手，交主会话）

- **R1 计数器口径**：「移动计数器」认 `+=` / `= x + n` / `++`。若将来改成 `counts[type] = counts[type] + 1`（Map 下标写入）仍能过（走 `= x + n` 分支），但若改成 `counts.add(kind)`（集合 API）则判红——刻意 fail-closed，需要时补一条形态。
- **R2 一跳只看同文件**：helper 若搬到另一个文件，`arm_accumulates` 会判红（误伤）。触发条件：计数逻辑真的跨文件拆分时再扩（跨文件需要符号解析，装置比例不合算）。
- **R3 R12 只认 `opt|get(JSONObject)("mediaRef")`**：若序列化形态改成 `value["mediaRef"]` 下标读取，`NESTED_READ_RE` 失明 → R12 报「没读」= 红（fail-closed，不会静默放行）。
- **R4 语言方言**：`brace_balanced_arm` 数 `{}` 不跳过字符串字面量内部的花括号（该 face 的分支体里目前没有）。触发条件：分支体出现含 `{`/`}` 的字符串字面量（如 URL 模板）。
- **R5 兄弟判据未动**：R7/R8/R9/R10 等 8 条判据本轮未审（不在本门见证范围），它们的拼写敏感度另案。
- **R6 precision 面**：`MEDIA_PRECISION`（`dropMedia` 同一行同时含 image 与 video 名字）仍按「不区分」豁免；若将来出现「同一分支同时计两种」的计数面，本判据会把它排除在家族外（现状正确，未来需复核）。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m5
python3 scripts/scan/parts_json_funnel_check.py .            # R11/R12 双 PASS
python3 scripts/scan/parts_json_funnel_check.py --selftest   # PARTSJSON_FUNNEL_SELFTEST=PASS
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-B.md --repo "$PWD"   # → GATE_CAUGHT_WITNESS
sh scripts/scan/scan.sh && python3 scripts/scan/test_scan.py            # 112/0 + ALL 344
```

单点手工复现（打回见证 → 应 rc=1 → 还原）：

```bash
F=src/android/app/src/main/java/com/rikkaminis/app/share/ChatPartsAttachmentCount.kt
cp "$F" /tmp/cpa.bak
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
pairs = [
 ('            "mediaRef" -> {\n                if (isVideoMediaRef(value)) vid += 1 else img += 1\n            }',
  '            "mediaRef" -> Unit'),
 ('            "toolResult" -> {\n                val media = value?.optJSONObject("mediaRef")\n'
  '                if (media != null) {\n                    if (isVideoMediaRef(media)) vid += 1 else img += 1\n                }\n            }',
  '            "toolResult" -> {\n                value?.optJSONObject("mediaRef")\n            }'),
]
for old, new in pairs:
    assert s.count(old) == 1, old[:40]
    s = s.replace(old, new, 1)
open(p, "w", encoding="utf-8").write(s)
PY
python3 scripts/scan/parts_json_funnel_check.py .   # 应 rc=1：R11 "MOVE NO COUNTER" + R12 "DISCARD"
cp /tmp/cpa.bak "$F"
```
