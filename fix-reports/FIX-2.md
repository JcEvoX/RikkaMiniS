# FIX-2 · deep-audit-1044（v23 波）· A-02 `start()` 抢占后仍结算他人状态

- **分支**：`fix/dv24-a2-terminal-ownership`
- **基线**：main `f94413f52fa5403c903902f6b1d2ae10cb9bc906`（门 109 / 夹具 336）
- **判级**：P1（第二道门已核证；本支复核成立，判级不变）
- **停止节点**：分支 CI 绿（不合并 main）

---

## §1 缺陷与根因

### 症状原话（机制复述）

终端页在 `stop()` 之后迅速重开（或 `PRootKernel.boot()` 期间重入 `start()`）：界面停在 **STOPPED**，或更糟——输入、resize 全失效，PTY 里那个 bash 成了没人回收的孤儿进程。

### 根因假设（一句话）

`TerminalSession.start()` 的协程在**输掉代数竞争**之后，仍然去结算自己已经不再拥有的共享状态（`_state` / `termuxSession` / `liveSessions`）。

### 证据（基线 blob `f94413f5`，行号与任务书逐条一致）

```
105:    private val startGeneration = AtomicInteger(0)
129:        val myGeneration = startGeneration.incrementAndGet()
147:                if (myGeneration != startGeneration.get()) { … return@launch }   ← 只 bail，正确
183:                if (myGeneration != startGeneration.get()) {
186:                    _state.value = State.STOPPED          ← 缺陷①：覆盖新一代的 BOOTING/RUNNING
228:                if (myGeneration != startGeneration.get()) {
231:                    termuxSession = null                  ← 缺陷②：无 `=== session` 守卫
232:                    _state.value = State.STOPPED          ← 缺陷①
276:    fun stop() { 279: startGeneration.incrementAndGet() … 286: STOPPED        ← 代数 bump 方写状态
```

两条都能在真树上机械确认：

1. **状态覆盖**：`stop()`（`:279` 先 bump、`:286` 再写 STOPPED）与新一代 `start()`（`:127` 写 BOOTING）**都已经写了自己那一份状态**。旧协程在 `:186` / `:232` 无条件再写 STOPPED ⇒ 落在新一代的 BOOTING/RUNNING 之上 ⇒ 屏幕判定终端已死。
2. **槽位清除**：`:231` 的 `termuxSession = null` 没有 `if (termuxSession === session)`。新一代 `start()` 在 `:190` 写入自己的 PTY 后，旧协程到达 `:228` 检查点 ⇒ 把**新一代的 PTY 引用**清成 null。后果：UI 写不进、resize 不进（`termuxSession?.updateSize` 落空），`write()` 无目标；`:191` 加入的 `liveSessions` 弱引用也无人清理（会话收不到 TZ / 代理广播）。
3. **同族已修对照**（形态可复用、本支沿用）：catch 块 `:265-271`（`if (termuxSession === createdSession)`）、`onSessionFinished` `:631-636`。⇒ 这类"结算前先证明归属"的规则**在同一个文件里已经存在**，只是两个抢占早退点没接上。

### 门为什么没拦住（门级根因）

基线门 `claim_release_guard.py` 的 `is_ownership_cond()` 只看"条件里出现了什么词"，不看**方向**：

```python
OWNERSHIP_LONG = re.compile(r"(?i)(epoch|token|claim|generation|serial|owner|job)")
…
if op in ("===", "!=="): return True
if (OWNERSHIP_LONG.search(left) or …): return True
```

`myGeneration != startGeneration.get()` 里含 `generation` ⇒ 被判成「归属守卫」⇒ 它俩身后的两处无守卫结算被认证为"gated"。真树实测确实绿：

```
$ python3 scripts/scan/claim_release_guard.py .      # 基线
✅ Claim/release guard: no ungated release, self-bound gate, unguarded settlement or unminted claim found   (rc=0)
```

第二条盲区：`terminal_settlement_findings()` 只扫 `fun onSessionFinished` 的函数体，`fun start` 里的抢占早退点不在任何判据的覆盖面上。

---

## §2 修法（停在第 7 级：显式归属守卫）

**第 7 级**：不新增抽象、不新增状态、不新增第三份"取消标记"，而是把文件里**已有的归属规则**接到两个漏掉的落点上（`change-ladder` 第②级"代码库里已有"）。

### 生产改动

`:183` 抢占（PTY 已建、尚未挂到槽位）：

```kotlin
if (myGeneration != startGeneration.get()) {
    Log.i(TAG, "start() superseded before attach — killing PTY")
    killTermuxProcessTree(session)
    return@launch                     // ← 删去 `_state.value = State.STOPPED`
}
```

`:228` 抢占（PTY 已挂到槽位、尚未发布 RUNNING）：

```kotlin
if (myGeneration != startGeneration.get()) {
    Log.i(TAG, "start() superseded before RUNNING — killing PTY")
    killTermuxProcessTree(session)
    if (termuxSession === session) {                   // ← 新增实例同一性守卫
        termuxSession = null
        liveSessions.removeAll {
            it.get() === this@TerminalSession || it.get() == null
        }
    }
    return@launch                     // ← 删去无条件的 slot 清除与 STOPPED 发布
}
```

**核心不变量**（写进 KDoc）：**代数 bump 方负责发布状态**。`start()` 发布 BOOTING、`stop()` 与 `onSessionFinished` 发布 STOPPED —— 三者都在 bump 的同一条路径上，所以"被抢占方"永远不需要写 `_state`。这一条同时把"`stop()` 期间被抢占时最终态必须收敛 STOPPED"保住：收敛由 `stop()` 自己完成（L3 有专门用例 `staleAfterAttachWhenStopPreemptedConvergesToStopped`）。

`liveSessions` 清理放在同一守卫内：守卫命中的前提是槽位仍是我的 PTY，此时新一代 `start()` 一定还没走到 `:191` 的登记（它先写槽位再登记）⇒ 清理不会误删新一代的弱引用；`slot !== session` 时一律不动。

### 被否决的方案

| 方案 | 否决理由 |
|---|---|
| 在早退分支里 `if (startGeneration.get() == myGeneration) _state.value = STOPPED` | 该分支前提就是 `!=`，条件恒假 = 死代码，只是把"不写状态"打扮成"有条件地写"。任务书原话正是"等价于不写"，不如直接不写。 |
| 新增第三个状态（如 `SUPERSEDED`）或 `cancel()` 协程 | 需要所有消费者（屏幕 / prefill / AndroidView attach）同时理解新状态；而"代数 bump 方发布状态"已经足以自洽。改交互结构而非接单源（同 1043 FIX-9 的判断）。 |
| 把 `termuxSession` / `_state` 换成 `AtomicReference` CAS | 只把竞争窗口缩小到指令级，不消除"我不再是 owner 却在结算"的语义错误；且 `_state` 是 `MutableStateFlow`（UI 订阅面），换类型要动屏幕。 |
| 在 `stop()` 里加"等旧协程退出" | 会让 `stop()` 变成挂起函数，调用点（UI 返回键 / `onDestroy`）全部要改；半径远超本单。 |

---

## §3 改动清单

| 文件 | 增 | 删 | 说明 |
|---|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/sandbox/TerminalSession.kt` | 28 | 3 | 两个抢占检查点 + KDoc（`:183-197`、`:236-262`，行号为改后） |
| `scripts/scan/claim_release_guard.py` | 436 | 41 | 判据方向化 + 新 Check C3 + 运算符解析修复 + 8 个新夹具/变异 |
| 合计 | **464** | **44** | 2 文件 |

- **门数 / 夹具数不变**：109 门 / 336 夹具（0 新建门，只扩既有门 `claim_release_guard.py`）。
- 冻结面 `scripts/scan/scan.sh`、`scripts/scan/test_scan.py` **零触碰**（`git diff --stat` 只有上面 2 个文件）。
- 门自测臂 10 → **18**（14 个 breakers）。
- 跨支同文件：本支独占 `TerminalSession.kt` 与 `claim_release_guard.py`（`TRIAGE §5` 已划）。

---

## §4 门判据与带牙证明

### 判据改造 1：`is_ownership_cond(cond, taken=True)` —— 方向

新增 `taken` 参数：归属 = `==`/`===` 在**走 if 体**时成立，或 `!=`/`!==` 在**早退守卫**（`if (cond) return`）形态下成立。

- 走 if 体：`if (termuxSession === session) { …结算… }` → 归属 ✅
- 走 if 体：`if (myGeneration != startGeneration.get()) { …结算… }` → **不是归属**（"我已陈旧"）❌ ← 本缺陷的门级根因
- 早退形态：`if (genAtStart != generation) return` → 越过该 if 即"我还是 owner" → 归属 ✅（保住 `PersistentShell:778` / `ModelExecutionService:613` 这类既有合法形态）

### 判据改造 2：`compare_operands` 运算符解析

旧实现对每个运算符各跑一次 `finditer`，于是 `"!=="` 里也匹出一个 `"!="`，`"!="` 里再匹出一个 `"=="`（左操作数成了 `myToken !`）—— 实测：

```
>>> list(compare_operands("myToken !== claimToken"))
[('!==', 'myToken', 'claimToken'), ('!=', 'myToken', '= claimToken'), ('==', 'myToken !', 'claimToken')]   ← 幻影 "=="
```

那个幻影 `==` 让 token 形态的陈旧判定又被当成归属守卫（收紧后仍漏）。改为**单次交替扫描** `!==|===|!=|==`，一次一项。

### 判据改造 3：新 Check C3（`start_preemption_findings`）

`fun start` 体内的**抢占早退点**（条件极点证明"我不是 owner" + 块内含 `return`）里：

- **C3a**：不得出现 `_state.value = …`（bumper 已发布状态）；
- **C3b**：`termuxSession = …` / `liveSessions.remove…` 必须落在**实例同一性守卫**的块内（`ownership_spans()`，与 C1 共用）；
- **C3-ROT**：文件里声明了 `*generation*` 计数器 + 含 `termuxSession` 却没有 `fun start(` ⇒ 报 ROT（改名不能悄悄卸除判据）。

判据锚**决定点与极性**，不锚拼写：`myGeneration != startGeneration.get()` 与 `myToken !== claimToken` 两种拼法都能抓（各有一条带牙臂）。

### 带牙臂（selftest 18 臂，14 breakers）

| 臂 | 变异内容 | 预期 | 实测 |
|---|---|---|---|
| 1044-A02 pre-fix start() preemption settles unowned state | 基线抢占块（写 STOPPED + 无条件清槽） | C3a + C3b 双红 | ✅ |
| 1044-A02 pre-fix token-compared bail-out ALSO caught | 同上但条件为 `myToken !== claimToken` | C3b（liveSessions）红 | ✅ 证明不绑 `generation` 字面 |
| 1044-A02 post-fix start() bail-out is green | 修复后形态 | 绿 | ✅ |
| 1044-A02 start() preemption ROTs when the checkpoints vanish | `fun start` 改名 `fun begin` | `C3-ROT` | ✅ |
| mutation: slot clear loses its identity guard | 去掉 `if (termuxSession === session)` | 仅 C3b 红、C3a 绿 | ✅ |
| mutation: bail-out publishes state again | 抢占块里加回 `_state.value = STOPPED` | 仅 C3a 红、C3b 绿 | ✅ |
| mutation: guard on `== null` instead of identity | 守卫换成 `termuxSession == null` | C3b 红 | ✅ |
| direction: counter INEQUALITY is not an ownership gate | 同族两处释放，一处 `===` 守卫、一处 `!=` 计数器 | Check A 只报后者 | ✅ |

**判据回退变异（门级）**：把 `if same != taken: continue` 那一行删掉（= 回到基线的方向盲判据），复跑 selftest：

```
[FAIL] 1044-A02 pre-fix start() preemption settles unowned state (C3): missing '[C3] ' …
[FAIL] 1044-A02 pre-fix token-compared bail-out ALSO caught …
[FAIL] 1044-A02 mutation: slot clear loses its identity guard …
[FAIL] 1044-A02 mutation: bail-out publishes state again …
[FAIL] 1044-A02 direction: counter INEQUALITY is not an ownership gate (A) …
SELFTEST FAIL (5 arm(s))
```

⇒ 方向规则本身是被钉住的，不是装饰。

**三态实证**：真树绿（rc=0）· 基线 blob 红（4 findings：C3a×2 + C3b×1 + Check A `termuxSession` 无守卫×1）· 修复树绿。

---

## §5 验证四层

### L1 门

| 项 | 命令 | 结果 |
|---|---|---|
| 本门真树 | `python3 scripts/scan/claim_release_guard.py .` | ✅ rc=0 |
| 本门 selftest | `… --selftest` | ✅ `SELFTEST=PASS (18 fixtures: 14 breakers red, compliant/good arms green)` |
| 基线 blob | 同上，树 = `git show f94413f5:…/TerminalSession.kt` | ❌ rc=1，4 findings（见 §4 三态） |
| 全量门 | `gate_run.sh --exec -- sh scripts/scan/scan.sh` | ✅ **`Summary: 109 passed, 0 failed`**、`[1/109]..[109/109]` 109 条 |
| 夹具 | `python3 scripts/scan/test_scan.py` | ✅ **`ALL 336 CASES PASS`**（337 个 ✅ 计数含汇总行） |
| 本门夹具单跑 | `python3 -c "…t.test_claim_release()"` | ✅ 7/7（含 Check A 夹具、hand-off、`--report`） |

> 沙箱实况（非本支缺陷）：`scan.sh` 直跑与 `test_scan.py` 第一次都被 S28（`Function not implemented`）打断（`test_scan` 崩在 `test_da1025_gates` 的 `subprocess.wait`），改用 `gate_run.sh` 持锁重跑后全绿。登记 R9。

### L2 邻门（按概念反查，非任务书臆造名）

`grep -rln TerminalSession scripts/scan/*.py` 只命中本门本身 ⇒ 按**概念**选"释放/生命周期/闭环"家族 5 道：

```
exec_gate_release_check      rc=0
scheduled_store_release_guard rc=0
liveness_static_guard        rc=0
stale_closure_guard          rc=0
loop_counter_reset_guard     rc=0
```

**5/5 rc=0**。

### L3 真跑（真 kotlinc 2.0.21 + 真 JUnitCore，PRE/POST 双臂 + 3 变异臂）

生产文件本身不可 JVM 编译（`android.content.Context` / `Handler` / `Looper` / `com.termux.*` / `PRootKernel`）。但**抢占分支是自足语句块**，所以不写"源码形状测试"，而是**把真代码抽出来跑**：

- `probe/l3/extract.py`：从任意 blob 的 `fun start` 里按**门的 staleness 判据**（直接 `import claim_release_guard`，避免抽取器与门各说各话）取出 3 个抢占早退语句，`return@launch → return`、`this@TerminalSession → this@Harness`，塞进 harness（`AtomicInteger` / `CopyOnWriteArrayList<WeakReference>` / `_state` 桩），生成 `checkpoint1/2/3`。
- `probe/l3/TerminalSessionPreemptGuardTest.kt`：7 个用例。

| 臂 | 结果 |
|---|---|
| POST（工作树） | ✅ `OK (7 tests)` |
| PRE（`git show f94413f5:…`） | ❌ `Tests run: 7, Failures: 3` —— **精确红在缺陷点**：<br>`1) staleAfterAttachDoesNotPublishState` expected:\<BOOTING\> but was:\<STOPPED\><br>`2) staleBeforeAttachDoesNotPublishState` expected:\<BOOTING\> but was:\<STOPPED\><br>`3) staleAfterAttachKeepsTheNewerPty` expected same:\<Object@…\> was not:\<null\> |
| 变异 m1 `publish-state`（把 STOPPED 写回 `:183` 分支） | ❌ `Failures: 1` = `staleBeforeAttachDoesNotPublishState` |
| 变异 m2 `unguard-slot`（去掉 `=== session` 守卫） | ❌ `Failures: 2` = `staleAfterAttachKeepsTheNewerPty` + `staleAfterAttachKeepsTheLiveRegistryEntry` |
| 变异 m3 `wrong-predicate`（守卫换 `termuxSession == null`） | ✅ `OK (7 tests)` ← **装置天花板，见下** |

**m3 的诚实结论**：槽位被占用时，"槽位为空"与该"槽位是我的"两种谓词的**行为完全等价**（都什么都不清）。构造不出合法场景让二者分叉：`liveSessions` 登记发生在写槽位之后（`:190` → `:191`），槽位一旦为空，说明新一代还没登记，清理是无害空操作。⇒ **该形状只有"形状判据"能看见**，由门的臂 `mutation: guard on '== null' instead of identity` 覆盖（红）。这也说明 C3b 的门判据不是行为判据的重复，而是补盲。

`mut-wrong-predicate` 因此保留在装置里当**对照臂**（预期绿），不进"精确红"清单。

**交替路径覆盖**（任务书 §4 要求）：`start 抢占 start`（`staleBeforeAttach*` / `staleAfterAttachKeepsTheNewerPty`）与 `stop 抢占 start`（`staleAfterAttachWhenStopPreemptedConvergesToStopped`）两条都覆盖；后者同时证明"最终态收敛 STOPPED 且旧 PTY 被杀"这条**必须保住**的语义。

### L4 CI

见 §8（run id / job id / 判读表）。

---

## §6 任务书失真

逐条回真树核对，**主要锚点全部对得上**（任务书 §2 的行号 105/129/147/183/228/276/279 与基线 blob 逐字符一致）：

1. **§2「报告称其正则为 `\b\w*[Gg]eneration\b\s*[=!]=`，实际实现是词表」** —— 任务书自己已标注这一层，实测确认为词表（`OWNERSHIP_LONG`/`OWNERSHIP_SHORT`）。**但真相比这两者都更糟**：`compare_operands` 会从 `!==` 里再匹出一个幻影 `==`（§4 改造 2），所以即使改成"词表 + 方向"也仍会漏 token 形态。已在门里修掉，记为任务书未预见的第三层。
2. **§2「`:183-188` 旧协程无条件把 `_state` 写成 `STOPPED`，会覆盖新 `start()` 刚写的 `BOOTING`（甚至已写到的 `RUNNING`）」** —— 成立，但**触发条件的措辞略重**：`start()` 开头有 `if (_state.value == State.RUNNING) return`，所以"覆盖已 RUNNING"要新 `start()` 已经推进到 `:235`（极窄）；实操面主要是"覆盖 BOOTING"。本支按机制修，不按概率修，结论不变。
3. **§4「门判据回退到旧 `is_ownership_cond` → 门臂红」** —— 成立且已实证（§4 末，5 臂红）。
4. **§2「`:228-234` … 且遗漏清理 `:191` 加入的 `liveSessions` 弱引用」** —— 成立。补一条任务书没说的事实：`liveSessions` 存的是**本 `TerminalSession` 对象的弱引用**（不是 PTY），所以"清理"语义是"摘掉本对象"，天然会连带摘掉同一对象新一代的登记 —— 因此清理**必须**和槽位守卫绑在一起（本支就是这么做的），否则会变成新缺陷。已写进 §2 与 KDoc。

---

## §7 未做 / 残留（每条带触发条件）

- **R1｜真机行为级（必须真机验）**：终端页快速「关 → 开」10 次，观测①是否出现卡在 STOPPED 的界面（日志应见 `start() superseded before … — killing PTY` 且随后新一代正常 RUNNING）；②`ps -ef | grep proot` 是否残留孤儿 bash；③重开后输入/resize 是否立即生效。**触发条件** = 下一次真机窗口。**触发后方案** = 主会话汇总进 `§1044-VERIFY`。
- **R2｜`start()` 重入守卫缺失（P2，本单半径外）**：`start()` 只在 `_state.value == RUNNING` 时提前返回，**BOOTING 期间重入会再起一条 boot 协程**（本缺陷的多发场景正是它）。真正的单源守卫应是"BOOTING 已在进行中则复用/忽略"。**触发条件** = 真机 R1 观察到同一个 `TerminalSession` 出现两条 `start() superseded` 日志，或出现两次 PTY 创建。**触发后方案** = 在 `start()` 入口加 `if (_state.value == BOOTING) return` 并补对应门判据（需另开单，因其改变交互语义）。**搁置理由** = 改的是入口语义不是结算语义，超出 A-02 工单面；且没有触发证据表明当前用户路径会重入。
- **R3｜`liveSessions` 以对象为键而非以 PTY 为键（P3）**：同一 `TerminalSession` 对象多代 PTY 共享一个登记项，语义上是"对象级"而非"会话级"。**触发条件** = 出现"同一对象先后两次 RUNNING 且只有后一次收到 TZ/代理广播"的报障。**触发后方案** = 改为 `WeakReference<PTY>` 或加代次标记。**搁置理由** = 当前 `broadcast*` 只按对象去重、且登记/摘除都已在主线程串行，未观测到影响。
- **R4｜C3 只覆盖 `fun start`（P3）**：同族 `fun resume` / `fun restart` 之类若将来出现抢占早退点，C3 不会看。**触发条件** = `TerminalSession`（或同类）新增第二个带代数检查的启动函数。**触发后方案** = 把 `START_FUN_RE` 放宽成"含 generation 比较的函数"集，并补 ROT。**搁置理由** = 当前树只有一个。
- **R5｜C3b 只锚 `termuxSession` / `liveSessions` 两个键（P3）**：若终端家族新增共享槽位（如 `attachedView`），判据不会自动覆盖。**触发条件** = 新增第三个跨代共享槽位并在抢占块里释放。**触发后方案** = 扩 `SLOT_CLEAR_RE` 家族并在 `TERM_SETTLE_KEYS` 里登记。**搁置理由** = `attachedView` 目前只在 `stop()` 里清，不在抢占块。
- **R6｜门 selftest 的夹具计数已变（10 → 18）（信息项）**：`test_scan.py` 对本门只跑真树 + 独立夹具，不校验臂数，故冻结面无影响；但若将来有人给本门补"selftest 臂数"断言，需同步。**触发条件** = 给 `claim_release_guard` 加 selftest 计数断言。**触发后方案** = 一起改。
- **R7｜L3 装置未入库（P3）**：`probe/l3/`（`extract.py` / `arm.sh` / 测试 / 5 臂产物）被 `.gitignore:141` 排除。**触发条件** = 下一支需要同类"从真源码抽语句块"的装置。**触发后方案** = 按本报告 §5 重建（`extract.py` 可直接复用，它 `import` 门本身做 staleness 判定）。**搁置理由** = 本仓惯例。
- **R8｜沙箱 S28 打断本地全量（环境，非本支缺陷）**：`scan.sh` 直跑与 `test_scan.py` 首次均被 `Function not implemented` 打断（后者崩在 `test_da1025_gates` 的 `subprocess.wait`；`tail`/`sleep`/`/dev/null` 同时不可用，`cd` 后的 cwd 追踪把 JVM 打挂）。**触发条件** = 收口期需要本地全量证据。**触发后方案** = 用 `gate_run.sh`（持锁 + 重试）并避免脚本内 `cd`。**搁置理由** = CI 跑同一份脚本，全量证据由 CI 提供。
- **R9｜C3 的 `_state` 判据只认 `_state.value =`（P3）**：若将来用 `_state.emit(...)`/`_state.compareAndSet(...)` 改写，判据会失明（不会误报，会漏报）。**触发条件** = `_state` 改用非赋值 API。**触发后方案** = 扩 `STATE_PUBLISH_RE`（与 C1 的 `SETTLE_DECISIONS` 一起改，两处共用同一族正则）。

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906` |
| 本地 HEAD | 见下方"收口回报"（本报告随最后一个提交入库，源码面此后不再变） |
| `git ls-remote origin <branch>` | 与本地 HEAD 逐字符一致（推送后核对） |
| CI `run.head_sha` | 与上两者逐字符一致（dispatch 后核对） |
| CI run / job | run id / job id 见收口回报 |

**源码树等价声明**：若本报告有回填提交，则以 `git diff --name-only <CI 绿 sha> HEAD -- . ':(exclude)fix-reports'` 输出为空作为"CI 结论对当前代码树成立"的证据（`fix-reports/` 不在 workflow paths 内，不参与构建）。
