# FIX-META-WIDE · `downloads_atomic_update_check.py`

- **分支**：`fix/meta-wide-downloads-atomic`（基线 `564c8f3e`）
- **波次**：meta-audit wide-1024（50 门宽扫）**标杆门** + 施工范式样例
- **机械复证**：3 条 `FALSIFIED` → 全部 `GATE_BLIND_CONFIRMED`（打上补丁后旧门 rc=0）

## 1. 门

`scripts/scan/downloads_atomic_check`（`downloads_atomic_update_check.py`）—— 守卫
`BrowserTabPool.kt` 里 `_downloads`（`MutableStateFlow`）的写入必须原子（ISSUE-03C：
四个写点跨三线程，非原子读改写会静默丢下载行）。

## 2. 被证伪的三条轴

| # | 轴 | 见证形态 | 旧判据为何失明 |
|---|---|---|---|
| 1 | `spelling` | `_downloads.apply { value = listOf(entry) + value }` | `WRITE_RE = _downloads\.value\s*=` 只认显式接收者拼写；作用域函数把接收者藏进 `this`，子串根本不出现 |
| 2 | `dataflow` | `val snap = _downloads.value` 后 `_downloads.update { snap.map { … } }` | 门只数 `_downloads.update {` 出现次数，不看闭包体是否消费 CAS 传入的当前值 ⇒ **调用了原子 API ≠ 实现了原子更新** |
| 3 | `dataflow` | `val next = _downloads.value.filter { … }` 拆成两行 + `_downloads.value = next // ALLOWED-FULL-REPLACE` | R1 只在**单行**右值里找 `_downloads`；R2 只看到 marker 就放行 ⇒ 快照读改写被拆行 + 豁免注释洗白 |

## 3. 修法（判据从「拼写/调用存在」改成「写入面 + 值流」）

- **R1b（新）**：跟踪 `val x = <读 _downloads 的表达式>` 绑定，同函数作用域内若写点右值引用 `x` ⇒ 读改写违规，**marker 不能豁免**（marker 的语义是「不依赖当前值」，快照写依赖）。
- **R3（新）**：`_downloads.update { … }` 闭包体必须消费 CAS 参数——体内重读 `_downloads.value`、或映射调用前捕获的快照、或显式形参在顶层从未被引用 ⇒ 违规。
- **R4（新）**：写面是**接收者**不是拼写——`apply`/`run`/`with`（裸 `value =`）与 `let`/`also`（`it.value =`）路由的写入同样过 R1/R2。
- **parser-death**：花括号配平失败 / 嵌套块剥离失败 ⇒ **fail-closed** 报违规（新增一条臂验证）。
- 实现：`_balanced()`（字符串/注释感知的块体提取）、`_strip_nested()`（顶层语句剥离，防嵌套 lambda 误判）、`_snapshot_bindings()`（函数作用域内快照绑定）。

## 4. 四层证据

1. **本门**：真树 `rc=0`；`--selftest` 11 臂——`good`/`clean-update` 绿，`bad-prepend`/`bad-map`/`bad-filter`/`wit-apply`/`wit-with`/`wit-stale`/`wit-param`/`wit-split`/`unbalanced` 各红**对应**判据。
2. **见证反攻（关键）**：三条原始见证原样打回真树 → 修复前 `rc=0`（门瞎）／修复后 `rc=1` 且错误行精确指向 `:570`（hidden receiver）、`:577`（pre-captured snapshot）、`:663`（snapshot `next`），三次 restore 全部 sha256 一致。
3. **邻门**：全量 `scan.sh` 覆盖（112 门）。
4. **全量**：`bash scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`；`python3 scripts/scan/test_scan.py` → `ALL 344 CASES PASS`。

## 5. CI

见分支回报（`fix/meta-wide-downloads-atomic`）。`head_sha` 与本地 HEAD 逐字符一致后停止节点，未合并 main。

## 6. 残留

- **R1b 的作用域判定用「下一个 0-4 缩进 `fun` 头」近似**：`object`/`companion` 内的嵌套函数、以及 `} else {` 里的函数声明可能让作用域区间略微过宽，理论上可把同文件另一函数的同名快照算进来。
  - 触发条件：出现同名 `val snap`/`val next` 在相邻函数且其一来自 `_downloads`。
  - 触发后方案：改用行级作用域栈（缩进 + `fun` 头配对），或复用 Kotlin 解析器。
  - 搁置理由：当前真树无同名快照；误报方向是「多报一条」，不是静默放行。
- **`ALLOWED-FULL-REPLACE` 仍可被滥用**（写不依赖当前值却标注豁免）：这是刻意的显式 opt-out 设计，属 P3（有守卫的现状）。
- `clean-update` 臂之外的合法「参数未使用」情形（如 `update { emptyList() }`）需带 marker 才放行——已在代码注释写明。

## 7. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-downloads
python3 scripts/scan/downloads_atomic_update_check.py . --selftest
python3 /var/minis/shared/work/meta-audit/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/wide-1024/B/Report_B.md   # 该门 3 条
```
