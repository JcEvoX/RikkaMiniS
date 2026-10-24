# FIX-META-WIDE-loop_persist_slice_check

- 分支：`fix/meta-wide-06-turn-loop-speech`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：wide-1024 组 A（`Report_A.md`）· 3 条门瞎实锤
- 结论：3/3 见证由「rc=0（门瞎）」翻成「rc≠0（门抓住）」，真树仍绿，生产代码零改动

## §1 门

`scripts/scan/loop_persist_slice_check.py` — 守 AgentLoopEngine 的**两个块索引不许合成一个**：

| 索引 | 语义 | 作用域 |
|---|---|---|
| `turnStartBlockIndex` | 本回合第一个块的起点 | 单回合 |
| `loopState.unpersistedBlockStart` | 还没落库的第一个块 | 整条 run |

两者一旦被写成同一个值，续写分支（空回合恢复 / EOF 续跑）前已流式呈现的块会在下一次
persist 时被切掉，界面有内容、库里没有。门原来 7 条判据（R1–R7），均为文本级锚定。

## §2 轴

| 见证 | 判据 | 轴 | also_blinds |
|---|---|---|---|
| W1 | R1 `buildTurnParts_uses_unpersistedBlockStart` | dataflow | R2 |
| W2 | R4 `unpersistedBlockStart_advances_only_on_persist` | order | — |
| W3 | R5b `agentHistory_backfills_dbMessageId` | dataflow | — |

三条都属同一病根：判据锚「某个字符串/某次调用存在」，不锚「这个值是谁给它的、给定之后
被谁消费」。所以补丁只要保住文本形状，行为可以完全反过来。

## §3 见证（补丁 → 旧门 → 新门）

判定链固定为：同一补丁，修复前 rc=0，修复后 rc≠0，恢复后 sha256 一致。

| 见证 | 补丁要点 | 旧门 | 新门 | 新门读数 |
|---|---|---|---|---|
| W1 | `loopState.unpersistedBlockStart = turnStartBlockIndex` 插在 buildTurnParts 之前 | rc=0 | rc=1 | `AgentLoopEngine.kt:3004: the unpersisted index is set to 'turnStartBlockIndex' …` |
| W2 | 在 `if (persistedRowId != null)` 之前再加一次 `= loopState.allToolBlocks.size` | rc=0 | rc=1 | `:3035: the unpersisted index advances OUTSIDE a success guard …` |
| W3 | 把 `if (lastAssistantIdx >= 0)` 改成 `if (lastAssistantIdx < -1)` | rc=0 | rc=1 | `:3047: the dbMessageId backfill sits behind the unreachable guard …` |

W1 之所以能过旧门：R1 只看 buildTurnParts 的两个实参文本（第二实参仍是
`loopState.unpersistedBlockStart`，形状完美），R2 只看「有没有出现 turnStartBlockIndex」，
而新插的赋值行恰好**合法地**含这个词；W2：R4 只匹配「advance 在守卫块内」这一存在性，
多一处**守卫块外**的 advance 不影响它；W3：R5b 只要求守卫块里出现
`copy(dbMessageId = persistedRowId)` 这段文本，守卫条件是 `>= 0` 还是 `< -1` 它不看。

## §4 修法（三条新判据，全部从树派生，不锚拼写）

**R8 · 赋值白名单（dataflow）**
`loopState.unpersistedBlockStart` 的右值只允许 `0`（复位）或
`loopState.allToolBlocks.size`（推进到当前块数）。算术推进（`+=` / `-=`）与任何
从 `turnStartBlockIndex` 派生的右值一律红。理由：run 级索引的合法取值只有「从头开始」和
「从头推进到当前块数」两种，第三种取值语义上就是两个索引塌回一个。

**R9 · advance 必须落在成功守卫内（order/dataflow）**
每一处 `= loopState.allToolBlocks.size` 都必须被 `if (<x> != null)` 形态的守卫包住——
即「insert 返回了行号」这一成功信号。守卫条件不允许是字面常量（`if (true)` 视同无守卫）。
理由：insert 返回 null 时，这块内容一个字节都没进库，索引却前移了，重试永不覆盖。

**R10 · 回填守卫必须可达（dataflow + 区间域）**
`copy(dbMessageId = <v>)`：
1. `<v>` 必须是标识符（不是 `null`、不是字面量）；
2. 必须存在索引存在性守卫 `if (<idx> >= 0)`（或 `> -1` / `!= -1`）；
3. 守卫链上的每个条件必须在**已知区间域**下可满足。域从初始化式派生：
   `indexOfLast` / `indexOf` / `lastIndexOf` ⇒ `[-1, +∞)`，`.size` ⇒ `[0, +∞)`；
   把 `x >= 0` 换成 `x < -1` 这类比较，区间交为空 ⇒ 判「永假守卫」。
理由：`copy` 的文本存在性对「守卫永远不成立」完全无感——这正是 W3 的形状。

解析层：所有字符串字面量内容与注释先被掩码（字符串先扫，`"video/*"` 不会开块注释），
括号/花括号配平解析 `if (…)` 的条件与语句区间；解析失败一律 fail-closed 报违规。

## §5 四层证据

- **L1 本门**：`python3 scripts/scan/loop_persist_slice_check.py .` → rc=0；
  `--selftest` → 10 条 fixture（1 条 clean + 9 条违规），逐臂计数符合预期，收尾标记
  `SELFTEST=PASS loop_persist_slice_check (6 violating fixtures red, compliant fixture green)`
  原样保留（冻结面，见 §7 R3）。
- **L2 见证反攻**：3/3 `CAUGHT rc=1`，三次 `restore=True`（sha256 前后一致）。见 §3 表。
- **L3 邻门**：`turn_tail_finalize_order.py` rc=0、`chat_session_id_binding_check.py` rc=0。
- **L4 全量**：`bash scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`；
  `test_scan.py` 46 个夹具函数逐个独立进程执行全部 rc=0（`ALL 344` 等价）；
  CI 日志里 `ALL 344 CASES PASS` 亦已复核。

## §6 CI run

| 项 | 值 |
|---|---|
| run | `40135353367`（#2636） |
| job | `120995610275` / build |
| conclusion | success（attempt 1） |
| head_sha | `f6d9dc84bb8e79c695ac0edde57571bb08e2f7fa` == 本地 HEAD |
| steps | 26 步，`not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控） |
| 门读数 | `Summary: 112 passed, 0 failed`；门头 112 个互异编号 |
| 夹具读数 | `ALL 344 CASES PASS` |
| Kotlin 错误 | ` e: ` 出现 0 次 |
| 含 FAILED 的行 | 12 条，逐条核对全部是 PASSED 用例名子串（0 条真失败） |

判读注意：该 run 的日志归档里同一份 step 日志存在两份（门头出现 224 次、互异 112；
`Summary` 与 `ALL 344` 各 2 行），所以「恰一条」在这里表现为「同一行两份副本」，不是两次执行。

## §7 残留（三条，各带三件）

**R1 · R9 只认单条件守卫**
- 触发条件：真树出现复合守卫 `if (persistedRowId != null && x > 0)`，或早退式
  `if (persistedRowId == null) return` 后再 advance。
- 触发后方案：对 `&&` 做深度 0 拆分（任一合取项是 `X != null` 即算守卫）；早退式加
  「同一函数体内、advance 之前存在 `if (X == null) return|continue`」分支。
- 搁置理由：本波两条 advance 站点都是单条件守卫；扩判据没有验证面（写不出见证），
  且偏严方向安全（fail-closed 报红，不会静默放行）。

**R2 · R10 的区间域只建模三个 API**
- 触发条件：新见证用别的「返回 -1」API（如 `binarySearch`、`list.indexOfFirst` 之外的
  自定义查找）做永假守卫。
- 触发后方案：把域派生从「三个 API 名」改成「返回 -1 的查找族清单 + 自定义函数签名扫描」。
- 搁置理由：真树只用 `indexOfLast` / `indexOf` / `.size` 三处；R10 的其余两条
  （值是标识符、必须有索引守卫）已覆盖 W3 的主要形态。

**R3 · 冻结尾标记里的夹具数已过期**
- 触发条件：主会话收口或并集阶段统一改 `test_scan.py:2695` 的全等断言时。
- 触发后方案：把标记改成 `(9 violating fixtures red, compliant fixture green)`，同步
  `test_scan.py:2695` 的全等断言（两处必须同一提交）。
- 搁置理由：`test_scan.py` 是本波冻结面（PACKAGE §1/§3.2），标记字符串被全等断言钉死；
  单改任何一侧都会让 CI 变红。真实臂数在 `--selftest` 的逐行输出里已可见（10 行 fixture）。

## §8 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-06-turn-loop-speech
python3 scripts/scan/loop_persist_slice_check.py .            # rc=0
python3 scripts/scan/loop_persist_slice_check.py --selftest   # 10 fixtures, SELFTEST=PASS

# 单条见证（W3）：打补丁 → 必须 rc=1 → 恢复 → sha256 一致
python3 - <<'PY'
import hashlib, subprocess
p = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/AgentLoopEngine.kt"
s = open(p).read(); find = "if (lastAssistantIdx >= 0)"; assert s.count(find) == 1
h = hashlib.sha256(s.encode()).hexdigest()
open(p, "w").write(s.replace(find, "if (lastAssistantIdx < -1)"))
print("rc =", subprocess.run(["python3", "scripts/scan/loop_persist_slice_check.py", "."]).returncode)
open(p, "w").write(s)
print("restore =", hashlib.sha256(open(p, "rb").read()).hexdigest() == h)
PY

# 全量
bash scripts/scan/scan.sh 2>&1 | tail -3
```

15 条见证的统一复现脚本：`/var/minis/shared/work/meta-wide/w06_probe.py`（本支自建，
按门过滤：`python3 w06_probe.py loop_persist`）。
