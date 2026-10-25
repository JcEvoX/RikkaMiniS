# FIX-META-WIDE · offload_relative_path_unify_check（门见证加固）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 分支：`fix/meta-wide-04-offload`
- 范围：只改本门脚本；生产 `.kt` 零改动

## 1. 门

`scripts/scan/offload_relative_path_unify_check.py` —— 相对路径单一实现门（三个 handler，C/S/Z 三组判据）：

| 判据组 | 契约 |
|---|---|
| `C1`–`C4` | `ModelUseOffloadHandler`：相对路径只在 `resolveMaybeRelative()` 里拼，且拼出来的值用于读文件 |
| `S1`–`S4` | `SpeechOffloadHandler`：`--source` 经共享 join → 会话映射；mic 分支晚于显式 source 分支 |
| `Z1`–`Z4` | `ShizukuOffloadHandler`：交给沙箱外服务的路径必须**先**做 guest→host 会话映射 |

## 2. 轴

四条见证同一族：**「正确的 API 被点名，正确的值没有流动」**。

- **dataflow（入口侧）**：`resolveMaybeRelative(trimmed, null)` —— 调用在、参数表在、调度在，唯一缺的是 cwd 本身。
- **dataflow（出口侧）**：`raw.trim().also { resolvePathForSession(…) }` —— 两个调用字面量都在，返回值仍是 guest 拼写。
- **anchor → 可达性**：`if (false) return NativeOffloadResult(2, …)` —— 拒绝语句的全部 token 都在，永远不执行。
- **dataflow（跨站点）**：`val resolved = resolveMaybeRelative(…)` 之后把手里的**原始参数**交给下一跳。

## 3. 见证（逐字）

### 见证 8 · C1
`ModelUseOffloadHandler.kt:1056`
```
    private fun readLinuxPath(linuxPath: String, sessionId: String?, cwd: String?): String? {
        val resolved = resolveMaybeRelative(linuxPath, cwd)
        val hostFile: File = sessionAwareHostFile(resolved, sessionId) ?: return null
→       val hostFile: File = sessionAwareHostFile(linuxPath, sessionId) ?: return null
```
旧门为什么绿：C1 只问 `resolveMaybeRelative(` 在不在 `readLinuxPath` 里。
真实后果：`--input note.txt` / `--system-file …` / 图片路径全部按 `/note.txt` 找——相对路径永远读不到，且没有任何错误提示（`exists()` 为假 → `return null` → 上层当成「文件为空」）。

### 见证 9 · S1 / S2
`SpeechOffloadHandler.kt:264`
```
    val guestPath = resolveMaybeRelative(trimmed, cwd)
→   val guestPath = resolveMaybeRelative(trimmed, null)
```
旧门为什么绿：S1 只问调用在不在；S2 只问 `cmdTranscribe(args, request.sessionId, request.cwd)` 是否把 cwd 转发进 `resolveSourcePath(source, sessionId, cwd)`。
真实后果：`--source rec.wav` 在有 session cwd 的情况下落到 `/rec.wav`——文件存在也报「找不到」，用户唯一的结论是「语音转写坏了」。

### 见证 10 · S4
`SpeechOffloadHandler.kt:cmdTranscribe`（非 mic 显式 source 分支）
```
    return NativeOffloadResult(2, OffloadOutput.formatBody(body, args) + "\n")
→   if (false) return NativeOffloadResult(2, OffloadOutput.formatBody(body, args) + "\n")
```
旧门为什么绿：S4 在分支区间里找 `"error"` 载荷、取 `return NativeOffloadResult(<code>` 的 code、断言最后一个非 0。
真实后果：`--source` 的拒绝路径成为死代码，控制流落进 live microphone——用户要转写一个文件，应用开始录音。

### 见证 11 · Z2
`ShizukuOffloadHandler.kt:940`
```
    return try {
        PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)?.absolutePath
    } catch (t: Throwable) { … }
→   return try {
        raw.trim().also { PRootKernel.resolvePathForSession(request.sessionId, guestPath, context) }
    } catch (t: Throwable) { … }
```
旧门为什么绿：Z2 只问 `request.cwd` 在不在函数体里；Z3 只问 `resolvePathForSession(` 在不在。
真实后果：Shizuku 侧 `ls -la <guest 路径>` 对容器路径无意义——返回空/错误被当成「目录为空」，随后的 `rm`/`cp`/`pm install` 全部作用在错误的路径上。

## 4. 修法

新增六个解析辅助（本文件内）：

- `_bracket_close()`（`(`,`)` 与 `{`,`}` 两用，配不上 → -1）
- `_call_args(text, call)`（首个调用的顶层实参列表）
- `_join_symbol(body, call)`（`val X = call(…)` / `X = call(…)` → `X`）
- `_returns(body)`（每个 `return` 表达式；`try { … } catch { … }` 整体算一条）
- `_returned_from_translation(expr, body)`（返回表达式是否**由** `resolvePathForSession` 产生：链头形态或绑定到局部变量）
- `_dead_condition()` / `_dead_spans()`（`if (false…)` / `if (X && false…)` 的**死代码区间**）

| 判据 | 新判据 |
|---|---|
| `C1` | 仍要求调用在；再要求 `sessionAwareHostFile(...)` 的**第一个实参**就是 join 结果的绑定名（或内联调用），否则红 |
| `S1` | 仍要求调用在；再要求 join 的**第二实参**引用了 `cwd` 参数；再要求 join 结果是被 `resolvePathForSession(...)` 消费的那个值 |
| `S2` | 追加：cwd 已不再进入 join → 同样红（同一次见证打穿 S1/S2，这条如实记录双轴） |
| `S4` | 追加：拒绝 `return` 若落在死代码区间，红 |
| `Z2` | 追加：函数**返回的**值必须由 `resolvePathForSession` 产生（链头 / 绑定变量）；`also { … }` 这类「调了但返回原值」判红 |

selftest 新增 5 条用例：1 条 clean 形态（`sessionAwareHostFile(resolved, …)` 必须被接受）+ 4 条见证原样。收尾标记前缀 `OFFLOAD_RELATIVE_PATH_UNIFY_SELFTEST=PASS` 原样保留，计数文案更新为 `(18 fixtures: 16 violation shapes caught, 2 clean shapes green)`。

## 5. 四层证据

**L1**
- 真树：`python3 scripts/scan/offload_relative_path_unify_check.py .` → rc=0
- selftest：`--selftest` → 18/18 `[PASS]`，rc=0；既有 13 条**全部**保持原期望 id 集合（说明新判据没误伤旧臂）

**L2 见证反攻**

| 见证 | old rc | new rc | restore |
|---|---|---|---|
| C1 readLinuxPath maps the raw arg | 0 | 1 | True |
| S1/S2 speech join fed `null` cwd | 0 | 1 | True |
| S4 speech refusal is dead code | 0 | 1 | True |
| Z2 shizuku returns the guest spelling | 0 | 1 | True |

**L3 邻门**：`offload_media_b64_wire_check` / `offload_cli_args_check` / `offload_host_path_check` rc=0

**L4 全量**：`scan.sh` → `112 passed, 0 failed`（本地）；`test_scan.py` 全量在本机被沙箱 ENOSYS 窗口腰斩（S28），本地改跑本门所属夹具组（`selftest has teeth` 标记断言 + 真树绿）全 PASS，全量 `ALL 344 CASES PASS` 由 CI job 日志给出（见 §6）

## 6. CI

run 40135342177 (#2635) · conclusion=success · job 120995575313 · head_sha=23fd18ec28008ec6757983ef82a98819a9e56010

机械判读（job 日志 1,484,240 B 逐条核过）：
- 门头 112 条、编号 1..112 **互异**、分母一致 = `112`
- `Summary: 112 passed, 0 failed` 恰 **1** 条
- `ALL 344 CASES PASS` 恰 **1** 条
- 编译错误（行首 ` e: `）= **0**
- 含 `FAILED` 的 6 行逐条确认全是 **PASSED 用例名的子串**（如 `slot denied produces FAILED without calling provider PASSED`）
- 本门 selftest 标记在 CI 日志中出现 **1** 次（门自证在 CI 里有人见证）
- 发布步骤未执行（分支门控，日志中无 Release 上传段）

## 7. 残留（三件套）

1. **返回值通过 `?.let { … }` / `run {}` 包装的合法写法会判红**
   - 触发条件：有人把 `resolvePathForSession(...)?.absolutePath` 改成 `resolvePathForSession(...)?.let { it.absolutePath }`
   - 触发后方案：`_returned_from_translation` 增加「谓词是同一调用的接收者链」形态（`?.`/`.let`/`.also` 区分：`also` 返回原值属反面）
   - 搁置理由：当前树只有直链 + `try{}` 两种形态；fail-closed 方向安全，红一次就会有人来看。
2. **`C1` 只审 `sessionAwareHostFile` 这一个下一跳**
   - 触发条件：`readLinuxPath` 改成经第二个 helper（如 `openSessionFile`）间接映射
   - 触发后方案：改为「从 join 结果出发、在同文件内追一跳消费点」的通用派生
   - 搁置理由：本波零生产代码改动；且现状新增间接层会同时让 C1 的旧判据（调用在）与新的实参判据一起变红，不会静默。
3. **`_dead_spans` 不追跨语句的局部常量**（`val never = false; if (never)`）
   - 触发条件：真代码出现常量间接折叠
   - 触发后方案：同文件一跳 `val` 传播
   - 搁置理由：与 cli_args 门第 1 条残留同因，合并评估。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-04-offload
python3 probe/l2_verify.py            # 11/11 CAUGHT（本支三门合并）
python3 scripts/scan/offload_relative_path_unify_check.py . --selftest
python3 scripts/scan/offload_relative_path_unify_check.py .
```

> `probe/` 未入库（`.gitignore` 覆盖）；`l2_verify.py` 留在本机同名 worktree 的 `probe/` 下，等价手写复现 = 「打补丁 → `git show HEAD:scripts/scan/<门>.py > /tmp/old.py` 跑旧门（期望 rc=0）→ 跑新门（期望 rc≠0）→ `git checkout -- <file>` + `sha256sum` 核对」。
