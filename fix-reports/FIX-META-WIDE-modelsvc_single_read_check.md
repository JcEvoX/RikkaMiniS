# FIX-META-WIDE-modelsvc_single_read_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/modelsvc_single_read_check.py`，2 段判据（service 的「每次运行只读一次 body」/
  offload 的「有界 worker + accept 不自己起线程」），守护模型 worker 单读不变量与 native offload
  的并发上限。
- 收尾标记 `MODELSVC_SINGLE_READ_SELFTEST=PASS`（未改动）。

## 2. 轴
| # | 段 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | check_service 读点数 | spelling | 只数 `requestFile.readText()` 一种拼写；`JSONObject(requestFile.inputStream().bufferedReader().use { it.readText() })` 是**第二次全量读 + 二次解析**，判据看不见 |
| 2 | check_offload 许可 | dataflow | 只判 `permits.tryAcquire()` 在场；`… \|\| true` 把上界变成常量，每次 submit 都线程 |
| 3 | accept_loop_slice 线程 | spelling | 只找字面量 `thread(`；`kotlin.concurrent.thread { … }`（参数全默认、尾随 lambda）不含左括号 |

## 3. 见证
- W1 → `val req = JSONObject(requestFile.inputStream().bufferedReader().use { it.readText() })`（替换 `requestBody.jsonOrThrow()`）
- W2 → `permits.tryAcquire() || true`
- W3 → accept 循环里加 `kotlin.concurrent.thread { handleClient(client, 0L) }`

## 4. 修法
1. **读点按 API 集合计**：`readText|readLines|readBytes|inputStream|bufferedReader|useLines|
   readChannel|copyTo|lines(` 任一被 `requestFile` 接收即算一个读点，**全文 finditer**（同一行两次读
   也算两次）；读点必须恰为 1。另加 `JSONObject(requestFile…` 直建的第二解析点判据。
2. **许可值不得被短路**：`permits.tryAcquire()` 之后紧跟 `||` / `&&` / `?:` / `.` 即红；
   并新增「`if (spawn)` 必须存在」——判定结果要真的门控 spawn。
3. **线程按形态认**：`(?<!\w)thread\s*[({]`（含 `thread {`）、`Thread(`、`Executors` 全算起线程。

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/modelsvc_single_read_check.py .` → rc=0
- 自证：`python3 scripts/scan/modelsvc_single_read_check.py --selftest` → 收尾标记 `MODELSVC_SINGLE_READ_SELFTEST=PASS`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 ISSUE-12a (check_service)       rc=1 restore=True
[CAUGHT] #2 ISSUE-12b (check_offload)       rc=1 restore=True
[CAUGHT] #3 ISSUE-12b (accept_loop_slice)   rc=1 restore=True
```
**修复前同一批见证全部 rc=0（BLIND=20）**，修复后按门计：`CAUGHT=3 BLIND=0 ANCHOR_MISS=0 restore_all=True`。
判据链成立：同一把刀（外部给的、原样未改）当初打穿门，现在被挡住。

### L3 邻门（直跑，逐条 rc=0）
| 邻门 | 结果 |
|---|---|
| `mirror_category_route_check` | `MIRROR_CATEGORY_ROUTE_CHECK=PASS` |
| `provider_remember_key_check` | `[C-SV2b] PASS` |
| `models_provider_field_check` | `OK (6 arms clean)` |
| `memory_gate_provider_fields_check` | `clean` |
| `discarded_return_guard` | `encode verdict section green` |
| `provider_models_cache_wiring_check` | `GATE=GREEN` |
（本支六门互为邻门，六门同时绿见 §L1。）

### L4 全量
本地 `scan.sh` 在施工窗口内两次撞上沙箱 S28（`Function not implemented` 扩散到 busybox，
见 `shared/sandbox-limits.md`），按注册表纪律**不再本机重跑全量**，全量证据取分支 CI job
日志（`Summary: 112 passed, 0 failed` 恰一条 + `ALL 344 CASES PASS`）。逐门直跑（本门 6/6）
与邻门直跑（§L3）在本地完成，未被 S28 波及。


## 6. CI
（分支 CI 见本目录 `FIX-META-WIDE-INDEX.md` 的 run 表；`head_sha` 与 `git rev-parse HEAD`
逐字符一致才认绿。）

## 7. 残留
- **R1** 线程形态集不含 `Runnable { … }.start()`（无 `Thread(` 也无 `thread`）与 `ForkJoinPool`。
  ／触发条件：引入新并发原语。／触发后方案：把「并发原语白名单」抽成常量并加 `--selftest` 臂。
  ／搁置理由：本文件只用 `thread{}` 与 `Executors`。
- **R2** 短路判据只看 `tryAcquire()` 紧随的 8 个字符：`val ok = permits.tryAcquire()` 后再
  `if (ok || true)` 属于间接短路，漏。／触发条件：许可值经局部变量中转再被短路。／触发后方案：
  变量一跳追踪（`val <v> = permits.tryAcquire()` → 检查 `<v>` 的消费点）。／搁置理由：新增的
  `if (spawn)` 判据覆盖主要形态，且间接短路会让 `spawn` 变量名消失进而触发 fail-closed。
- **R3** 读点数按 API 集合计，`requestFile.absolutePath` 交给第三方读（如 `File(path).readText()`）
  不在集合内。／触发条件：改成传路径而非流。／触发后方案：把 `absolutePath` 也纳入「读点」并追一跳。
  ／搁置理由：当前唯一读者是 capture 与 witness 形态，`absolutePath` 只用于日志。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/modelsvc_single_read_check.py .
python3 scripts/scan/modelsvc_single_read_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only modelsvc_single_read_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。
