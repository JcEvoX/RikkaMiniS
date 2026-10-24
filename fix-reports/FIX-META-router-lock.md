# FIX-META-router-lock — 元审计见证固化（axis=spelling）

- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`（main）
- 分支：`fix/meta-audit-m5-scope-order`
- 门：`scripts/scan/router_lock_check.py`
- 轴：**spelling**（拼写）—— 锁的身份是否稳定，而不是锁的名字在不在
- 生产代码：**0 行改动**

## 1. 门在防什么

`GroupRouter` 持有一个进程级 `LinkedHashMap<String, MemberHealth> health`，被主线程（`select` / `isUsable` / `clearHealth`）与 IO 协程（`recordResult`）并发读写。串行化靠一个监视器对象 `healthLock`：所有访问都在 `synchronized(healthLock) { … }` 里。

门要防的假绿：**锁名字在，锁对象每次都是新的**。

## 2. 见证（外部对抗审计给出，已机械复证）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/data/routing/GroupRouter.kt",
 "find":"    private val healthLock = Any()",
 "replace":"    private val healthLock get() = Any()"}
```

自定义 getter 每次读取都新建 `Any()` ⇒ 每个调用点锁的是**不同对象**，互斥完全失效。而文本层面一切不变：`val healthLock` 声明还在、`synchronized(healthLock)` 四个站点还在、`HEALTH_REF` 引用的 span 覆盖仍然成立 —— **旧判据 13/13 全绿，rc=0**。真实后果即 ISSUE-10：`ConcurrentModificationException`、5xx 计数丢失导致熔断不打开、哈希链损坏。

## 3. 旧判据 → 新判据

旧判据里与锁对象身份相关的只有一条：

```python
declared = re.search(r"\bval\s+healthLock\b", code)   # ← 只证明名字存在
```

`healthLock get() = Any()` 与 `healthLock = Any()` 在「名字存在」这件事上完全等价，所以这条判据对身份无感；配套的 `synchronized_spans` 也只判「引用落在 span 内」，看不见 span 里锁的是哪个实例。

新判据 `HEALTHLOCK_IDENTITY_CRIT = "healthLock is a stable monitor (backing-field initializer, not a per-access getter)"`（`healthlock_identity()`）：

1. 定位 `val healthLock` 声明，取声明窗口（首行 + 紧随其后的 `get`/`set`/`by` 续行——自定义访问器两种写法都要覆盖：`val x get() = …` 与 `val x: Any\n    get() = …`）。
2. 窗口内出现 `get(…)` → **红**：每次读取新建监视器，`synchronized(healthLock)` 串行化不了任何东西。
3. 窗口内出现 `by` → **红**（fail-closed）：委托属性的实例身份无法静态确认（`by lazy { Any() }` 恰好是安全的，但静态不可判，宁可误红）。
4. 窗口内没有 `=` → **红**（fail-closed）：没有初始化器就无从确认 backing field。
5. 其余 → 绿（`val healthLock = Any()`，构造期求值一次，之后锁同一个实例）。

原有 4 类判据（声明存在 / 引用在 span 内 / `clearHealth` / IO 侧用法）全部保留，共 14 条。

## 4. 见证固化（`--selftest` 新臂）

`ROUTER_BREAKERS` 新增两条（marker `ROUTER_LOCK_SELFTEST=PASS` 不带 fixture 计数，冻结面无冲突）：

| 臂 | 形态 | 期望 |
|---|---|---|
| breaker 5 | `private val healthLock get() = Any()`（**外部见证原形**） | 红，恰好命中新判据 |
| breaker 6 | `private val healthLock by lazy { Any() }` | 红（fail-closed 委托形态） |

```
ROUTER_LOCK_SELFTEST=PASS (21 fixtures)
```

既有 breaker 1..4 与 `VACUOUS_ROUTER` fixture 期望未变（后者只断言「引用全部在 span 内」，新判据在其上判绿，不影响该 fixture 的既有红判据）。

## 5. 四层证据

| 层 | 内容 | 结果 |
|---|---|---|
| L1 | 真树 + `--selftest` | `router_lock GATE=GREEN`（14 条判据全绿）；selftest **21 fixtures PASS** |
| L2 | `verify_witness.py report-B` | `router_lock_check` → **`GATE_CAUGHT_WITNESS`**、`restore=True`；见证打回真树后报 `[FAIL] GroupRouter.kt:40 … custom getter (get() = …) — every read builds a NEW monitor`，还原后 `GATE=GREEN` |
| L3 | 全量 | `scan.sh` → **`Summary: 112 passed, 0 failed`**（exit 0）；`test_scan.py` → **`ALL 344 CASES PASS`**（exit 0） |
| L4 | 分支 CI | run **#2622**（§6） |

L2 双向：修前 `rc=0 → GATE_BLIND_CONFIRMED`，修后 `rc=1 → GATE_CAUGHT_WITNESS`，`restore=True`（生产文件逐字节复原）。

## 6. CI

- 分支 push 不触发（`paths` 不含 `scripts/**`），手动 dispatch `build-apk.yml --ref fix/meta-audit-m5-scope-order`。
- run **#2622** / `head_sha = f64914d216bc80dc74263f36ba65ae1b525ba289`（与本地 HEAD 逐字符一致）
- 期望：`Summary: 112 passed, 0 failed` 恰一条、`ALL 344 CASES PASS` 恰一条、唯一 skipped = `Publish`（main 门控）、` e: ` 0、游离 `FAILED` 0。

## 7. 残留登记（不动手，交主会话）

- **R1 只覆盖 `healthLock`**：`ScheduledNotificationStore.lock` 等其它监视器由各自判据管（那里已经钉死 `val lock = Any()` 的完整形态，不属于本门见证面）。
- **R2 `by` 一律红**：`by lazy { Any() }` 实际是安全写法，但静态不可判身份 ⇒ 刻意 fail-closed。触发条件：真出现安全委托形态且被误红时，改成白名单 `by lazy`。
- **R3 多行初始化器**：窗口取声明首行 + 访问器续行；若初始化器写成多行（`val healthLock =\n    Any()`），首行无 `=` 也无 `get(`/`by`，续行不以 `get|set|by` 开头 ⇒ 落进「no initializer → 红」。这是 fail-closed 方向的误红，不会静默放行。触发条件：出现多行初始化器。
- **R4 注释里提到 healthLock 的安全性依赖 `code_only()`**：`analyse_router` 在去注释视图上跑，KDoc 里的 `[healthLock]` 不影响判据；新增文本分析时必须保持这个前提。
- **R5 `synchronized` 之外的健康访问**：若有人绕过 `synchronized` 直接读 `health`（如 `health.size`），由既有 span 判据管；新判据不重复承担。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m5
python3 scripts/scan/router_lock_check.py --root .          # GATE=GREEN
python3 scripts/scan/router_lock_check.py --selftest        # ROUTER_LOCK_SELFTEST=PASS (21 fixtures)
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-B.md --repo "$PWD"   # → GATE_CAUGHT_WITNESS
sh scripts/scan/scan.sh && python3 scripts/scan/test_scan.py            # 112/0 + ALL 344
```

单点手工复现（打回见证 → 应 rc=1 → 还原）：

```bash
F=src/android/app/src/main/java/com/rikkaminis/app/data/routing/GroupRouter.kt
cp "$F" /tmp/gr.bak
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
old = "    private val healthLock = Any()"
new = "    private val healthLock get() = Any()"
assert s.count(old) == 1
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY
python3 scripts/scan/router_lock_check.py --root .   # 应 rc=1：healthLock … every read builds a NEW monitor
cp /tmp/gr.bak "$F"
```
