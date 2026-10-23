# dv25-FIX-4 · A-07（P1）`android-player` 载荷不是 JSON：一个命令两种格式

> 本报告由主会话收口时按 `backlog §1045-A07` + 分支提交 + CI 判读重建（支 4 会话未落盘报告文件）。

## 1 身份链

- 基线 main `1d44ec90`；分支 `fix/dv25-a4-player-json-contract`，1 提交（`4a0b6fa6` 生产 + 测试 + 门）
- 5 文件 **+844/−69**；门 109 / 夹具 336 不变（0 新建门，既有门 `offload_cli_args_check.py` 扩 Z1–Z4）
- CI run `39917119039`（#2537）success，`head_sha=4a0b6fa6` 三方逐字符一致（EXACT）
- ⚠️ 台账 §1045-A07 的 L4 段是**从 §1045-B04 复制粘贴**的（写成 run 39915172412 / head 95d84870 / PickerGroupResolutionTest 10/10）——那两个数字属 FIX-8。本报告按真实 run `39917119039` / `4a0b6fa6` 记录，台账已就地更正。

## 2 缺陷本体

`MediaPlayerManager` 七个命令（play/pause/resume/seek/stop/status/listSessions）全部返回**英文句子**（`"Playing 'x.wav' (session=s1, …)"` / `"Error: no session 's9'"` / `"Active media sessions (1): …"`），而 `OffloadOutput.formatBody` **只重排 JSON**（散文原样透传）⇒ `-c` / `--compact` / `-q` / `--quiet` 全是空操作，`jq` / `json.loads` 在成功路径上炸，错误判定只能靠 `startsWith("Error")` 嗅探；而唯一真走 JSON 的是**不该走的那条**（catch 里的 `{"error":"player_failed"}`）——同一命令：成功=散文、失败=JSON。

## 3 修法（阶梯第 7 级：抽纯核 + 接既有信封）

manager 改返回**字段**（新纯核 `offload/PlayerOutcome.kt`，Android 无关、可在裸 JVM 编译），由 `playerEnvelope` 组装**既有形状** `{ok, tool, action, data}` / `{ok:false, …, error, message}`（接 `AccessibilityOffloadHandler` / `BrowserUseOffloadHandler` / `SessionsOffloadHandler` 同款信封，不发明新方言）；拒绝给**机器码**（`no_session` / `not_playing` / `cannot_seek` / `file_not_found` / `path_unresolved` / `video_unsupported` / `play_failed`）；位置/时长给**原始毫秒**；**缺席字段一律省略**（发 0 会被读成「在开头」）；`listSessions()` **按 session id 排序**（`ConcurrentHashMap` 顺序未定义 ⇒ 两次 `list` diff 出假变更，同函数顺手修）。退出码语义不变（0 跑完 / 1 拒绝 / 2 参数错），`--help` 与 usage 不经过 `formatBody`。manager 的锁 / release / 路径解析语义一字未动。

## 4 门（扩既有 `offload_cli_args_check.py`，0 新建门）

既有 N1/N2 都是**文件内**判据（player 传变量 ⇒ N1 绿；散文来自另一文件 ⇒ N2 绿）——两层都看不见跨文件委托，这正是它活下来的原因。新增 **Z1–Z4**：从树**派生**「载荷委托到别的文件」的成员集（每个 `formatBody(` 第一个实参 → 裸标识符则下探一层局部 `val` 初始化式 → 只算**值位置**的调用）；Z1 委托者必须登记在 `PAYLOAD_FILES` 或带理由的豁免表、Z2 `PAYLOAD_FILES` 里的文件不得从别处取 `String` 载荷、Z3 豁免必须仍需要且理由非空、Z4 派生集不得为空。6 带牙臂（期望集手写）。

## 5 验证四层

- L1 本门真树 rc=0（`8/8 payload files` → `9/9`）+ `--selftest` PASS + **PRE 臂基线整树 rc=1，7 条 Z2 违规**（逐条指到七个 `MediaPlayerManager.*(): String`）+ `scan.sh` **109/0** + `test_scan` **ALL 336**
- L2 概念反查邻门 3/3 rc=0（`mediaplayer_release_guard` / `offload_host_path_check` / 本门）
- L3 真 kotlinc + JUnitCore 七臂：POST `OK (9 tests)` / PRE 9/9 全红 / m1 信封塞散文 6 红 / m2 去掉兜底码 1 红 / m3 缺席字段发 0 2 红 / m4 空列表丢 count 1 红 / m5 `data` 消失 6 红；变异只施加在副本树
- L4 CI #2537：26 步 non-success `[]`、唯一 skipped=Publish、门头 109 连续互异、恰一条 `109 passed, 0 failed`、`ALL 336`、`Z e: ` 0、游离 FAILED 0

## 6 两条规则级收获

1. **门的盲区常在「判据的视野边界」而非写法**：N1/N2 各自正确，但都只在文件内看；缺陷形状是跨文件返回值 ⇒ 扩判据方向是**扩大视野**（跨文件派生），不是加严同文件规则。
2. **文本派生必须区分「值位置」与「语句位置」，且先剥字符串字面量/原始串**：首版把 `formatBody(...)` 调用本身判成跨文件委托；`HELP_TEXT` 原始串里的 `text(` / `from(` 被读成函数调用；同名本地声明必须赢过跨文件声明。

## 7 残留

R1 真机行为级（七命令输出确为 JSON、`-q` 抽得出 data/error、list count 类型一致、越界/视频分别回码）· R2 `WeatherOffloadHandler` 仍回散文（P2 刻意，半径纪律）· R3 新委托型 handler 会被 Z1 自动红（机制化）· R4 门只判来源不判字段名语义（测试兜底）· R5 `position_ms`/`duration_ms` 是 Int 毫秒（P3 上游限制）· R6 报告编号与 1044 重号（本批统一改为 `dv25-FIX-*` 入库，已解决）。全带触发条件。

## 8 影响面（对外可见变更）

`android-player` 全部子命令输出改为 JSON 信封；`-c`/`-q` 从空操作变为生效。**靠 grep 英文句子的旧脚本会失效**（仓内无脚本依赖散文形态）。
