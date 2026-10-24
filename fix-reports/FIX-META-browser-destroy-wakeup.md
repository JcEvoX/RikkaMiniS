# FIX-META-browser-destroy-wakeup — 门见证收口（axis = anchor）

分支 `fix/meta-audit-m4-anchor-body` · 基线 `20a9fbadac1b0a78e3013e299c1694eaa2812e2e` · 支 M4
本轮只改**门的判据**（+ 见证固化成 selftest 臂），**生产代码一行未动**。

## 1. 门

`scripts/scan/browser_destroy_wakeup_check.py`（deep-audit-1019 ISSUE-08 的浏览器拆解唤醒门）。
原本 33 条判据，含 `analyse_manager`（destroy / onRendererGone 两个**函数体内**必须出现唤醒调用）
与 `analyse_slot`（拆解入口 `AsyncJsSlot.takeCurrent` 存在性）。

## 2. 轴

`anchor` —— 但同一门里两种锚的差别正好把这次的洞暴露出来：

- `analyse_manager` 已经是**体内**判定（`function_body(doc, "destroy")` 里找 needle）；
- `analyse_slot` 是**声明-shaped**判定：`SLOT_MUST = "fun takeCurrent()"`，只问这个子串在不在
  去注释源码里 —— 函数体内容是任意值。

于是"入口存在"被当成了"入口有效"。门证明的是 `takeCurrent` 这个**名字**还在，而不是
"它还会取出并清空槽位"。

## 3. 见证（外部审计给出，本轮机械复证）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/browser/AsyncJsSlot.kt",
 "find":"    fun takeCurrent(): T? {\n        val current = entry ?: return null\n        entry = null\n        return current.value\n    }",
 "replace":"    fun takeCurrent(): T? {\n        return null\n    }",
 "why_gate_green":"analyse_slot 只判断去注释源码里有子串 \"fun takeCurrent()\"（SLOT_MUST），完全不看方法体是否读 entry / 置空 / 返回 value。",
 "why_defect_back":"takeCurrent 恒 null 且不清空槽位 ⇒ BrowserUseManager.destroy()/onRendererGone 里 takeCurrent()?.complete(...) 全成空操作 ⇒ executeJS()/fetch() 协程在 tab 销毁或渲染进程崩溃时永不被唤醒（1019 ISSUE-08 的 30s/60s 死等重现）。"}
```

**修前**：见证补丁 → 门 `rc=0`（`SLOT_MUST` 子串依然命中，33/33 绿）。
**修后**：同一补丁 → 门 `rc=1`，`Summary: 33 passed, 3 failed`，红的正好是三条体判据，
声明型那条（`entry point is gone`）**保持绿** —— 因为它确实还在，红的是"还会不会干活"。

## 4. 现状判据 → 新判据

### 4.1 `analyse_slot` 从「名字在」改成「效果在」

```python
doc  = strip_comments(slot_src)
body = function_body(doc, "takeCurrent")        # 已有 helper（括号配平、跳过字符串）
if body is None:                                # fail-closed：抽不出体 → 四条全红
    ...
return [
    ("… teardown entry point is gone",            True,                        "AsyncJsSlot.kt"),
    ("… no longer reads the slot",                SLOT_READ_RE.search(body),   "AsyncJsSlot.kt"),
    ("… no longer clears the slot",               SLOT_CLEAR_RE.search(body),  "AsyncJsSlot.kt"),
    ("… no longer returns the taken value",       SLOT_RETURN_RE.search(body), "AsyncJsSlot.kt"),
]
```

三要件（缺任一即红）：

| # | 语义 | 正则 |
|---|---|---|
| 1 | **读**槽位（`val current = entry ?: return null` 或等价读） | `\bentry\b(?!\s*=(?!=))` —— 排除"纯赋值目标"（`entry = …`），保留 `entry ?:` / `entry?.value` / `val x = entry` |
| 2 | **置空**（take 只能把 entry 交出去一次） | `\bentry\s*=\s*null\b` |
| 3 | **返回取到的值**（拆解方要 complete 的那个 deferred） | `return\s+[\w?.]+\s*\.value\b` |

`SLOT_MUST` 常量保留但**不再是判据**（只作文档交叉引用），避免"再加一条同名子串检查"的回潮。

### 4.2 见证固化：真树 + 逐字补丁双臂

新臂不是拿字符串玩具跑，而是**读真实 `AsyncJsSlot.kt`**：

1. `real tree: AsyncJsSlot.takeCurrent` → 真树四条全绿（防止"fixture 绿、真树红"的伪证）；
2. `real tree: witness anchor occurs exactly once (found 1)` → 见证 find 串在真树里**必须恰好命中一次**。
   文件被改写导致 anchor-miss 时这条**直接炸**，而不是静默变成一个什么都没改的绿臂；
3. `real tree + witness patch: gate reds the gutted body` → 逐字打补丁后，红的恰好是**三条体判据**。

外加 4 条合成臂覆盖"只坏一半"的形态（读而不清 / 清而丢值 / 重新赋值当清空 / 声明改名），
以及一条 clean 臂（`red set []`）。每臂的期望红集都**手写自判据表**，不从运行结果反推。

### 4.3 判据数：33 → 36

`Summary: 33 → 36 passed, 0 failed`。门总数（112）与夹具总数（344）**不变** —— 本轮不新增门、
不触碰 `scan.sh` / `test_scan.py`（冻结面）。

## 5. 四层证据

**L1（本门）**
- 真树：`python3 scripts/scan/browser_destroy_wakeup_check.py .` → `rc=0`，`Summary: 36 passed, 0 failed`，
  收尾 `GATE=GREEN browser_destroy_wakeup`。
- `--selftest` → `BROWSER_DESTROY_WAKEUP_SELFTEST=PASS`。新增臂原文：
  ```
  [OK] clean fixture: takeCurrent reads+clears+returns -> red set []
  [OK] witness fixture: takeCurrent body returns null -> red set ['… no longer reads the slot', '… no longer clears the slot', '… no longer returns the taken value']
  [OK] breaker: takeCurrent reads but never clears the slot -> red set ['… no longer clears the slot']
  [OK] breaker: takeCurrent clears but drops the value -> red set ['… no longer returns the taken value']
  [OK] breaker: takeCurrent reassigns the entry instead of clearing it -> red set ['… no longer clears the slot']
  [OK] breaker: takeCurrent declaration removed -> red set ['… teardown entry point is gone', '… no longer reads', '… no longer clears', '… no longer returns']
  [OK] real tree: AsyncJsSlot.takeCurrent -> red set []
  [OK] real tree: witness anchor occurs exactly once (found 1)
  [OK] real tree + witness patch: gate reds the gutted body -> red set ['… no longer reads', '… no longer clears', '… no longer returns']
  ```

**L2（机械复证，必过）**
```
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py <report-B> \
        --repo /var/minis/shared/work/meta-audit/wt-m4
── browser_destroy_wakeup_check.py
   [1] -  rc=1  restore=True  → 门抓住了（见证无效）
        Summary: 33 passed, 3 failed
        browser teardown wake-up gate: RED — the ISSUE-08 contract is violated
```
同轮其余 5 门仍 `GATE_BLIND_CONFIRMED`（不在本支文件集）。`restore=True` = 复原后 sha256 一致。

**L3（全量）**
- `sh scripts/scan/scan.sh` → `rc=0`，`║ Summary: 112 passed, 0 failed`。
- `python3 scripts/scan/test_scan.py` → `rc=0`，`✅ ALL 344 CASES PASS`。

**L4（CI）** 见 §6。

## 6. CI

`scripts/**` 在 `build-apk.yml` 的 paths 之外 —— push 不触发，手动 dispatch。
`head_sha` 三方（本地 / `branches` API / `run.head_sha`）逐字符一致。

- run：**40100764322**（job `120892255460`，`event=workflow_dispatch`）→ `conclusion=success`
- `head_sha = 95a46f5b6656d18843742aaca63bec108d656aa4`，与本地 HEAD **逐字符一致**
- 判据（判读 1,483,573 B 日志）：
  - `Summary: 112 passed, 0 failed` 恰 1 条；门头 `[N/112]` 112 条、`1..112` 连续无缺无重
  - `ALL 344 CASES PASS` 恰 1 条
  - `BROWSER_DESTROY_WAKEUP_SELFTEST=PASS` 在册；日志里 `takeCurrent` 命中 8 行
    （真树 4 条判据 + 2 个拆解唤醒判据 + 门头），证明新判据在 CI 里真跑
  - 门自身 `Summary: 36 passed, 0 failed`（33 → 36，新增三条体判据）
  - 26 步中唯一非 success = `Publish to Releases` **skipped**（main 门控）；游离 `FAILED` 0、编译错 `e: ` 0
- 报告提交 `c1e0c423` 在 CI 之后：`git diff --name-only 95a46f5b HEAD -- . ':(exclude)fix-reports'` → **0 行**。

## 7. 残留登记（同族，本支不动手）

1. **同一门内仍有声明-shaped 判据**：`analyse_release_policy` 的
   `"fun canRunDeferred(" in doc and "fun release(" in doc and "fun track(" in doc`
   —— 把它掏空成 `fun canRunDeferred(x) = true`，门仍绿（`WebViewReleaseLatch` 的
   deferred-work guard 就是"要么跑要么取消"的那个决定点）。触发条件：下一次审计给出该载体的
   见证，或有人改 `WebViewReleaseLatch` 这三个函数的函数体。
2. **`analyse_manager` 的 needle 是调用子串，不是效果**：`"asyncJsSlot.takeCurrent()" in destroy`
   能证明拆解路径**调用了** take，不能证明 take 有效 —— 本次把有效性补在 slot 侧，
   两半合起来才成立。若将来 `takeCurrent` 被内联成别的方法，slot 侧判据会 fail-closed 红，
   必须同时改两处（已由 `declaration removed` 臂标注这一耦合）。
3. `URLDecoder.decode` 逃逸判据是**行窗**（`BACK=2 / FWD=4`）而非 body —— 已知形态，
   属另一波审计的登记项，不在本支半径内。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m4
python3 scripts/scan/browser_destroy_wakeup_check.py .              # 真树 → rc=0 (36/0)
python3 scripts/scan/browser_destroy_wakeup_check.py --selftest      # 全部臂 + SELFTEST=PASS

# 见证（手工复证）
python3 - <<'PY'
p='src/android/app/src/main/java/com/rikkaminis/app/browser/AsyncJsSlot.kt'
s=open(p,encoding='utf-8').read()
old=("    fun takeCurrent(): T? {\n        val current = entry ?: return null\n"
     "        entry = null\n        return current.value\n    }")
assert s.count(old)==1
open(p,'w',encoding='utf-8').write(s.replace(old,"    fun takeCurrent(): T? {\n        return null\n    }"))
PY
python3 scripts/scan/browser_destroy_wakeup_check.py .              # → rc=1，3 failed（体三条）
git checkout -- src/android/app/src/main/java/com/rikkaminis/app/browser/AsyncJsSlot.kt
```
