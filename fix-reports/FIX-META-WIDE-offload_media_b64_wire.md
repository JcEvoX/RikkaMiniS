# FIX-META-WIDE · offload_media_b64_wire_check（门见证加固）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 分支：`fix/meta-wide-04-offload`
- 范围：只改本门脚本；生产 `.kt` 零改动

## 1. 门

`scripts/scan/offload_media_b64_wire_check.py` —— 离屏媒体 base64 线格式门（5 条判据，跨 worker→gateway 两个进程边界的**编码-解码契约**）：

| 判据 | 契约 |
|---|---|
| `C1` | gateway 用 `java.util.Base64.getMimeDecoder()`（容忍 RFC 2045 折行），且**真的用它解码**并携带字节 |
| `C2` | worker 侧 `media_files` 的 `Base64.encodeToString(..., NO_WRAP)`（单行、标准字母表） |
| `C3` | 消费者不得用严格 `java.util.Base64.getDecoder()` |
| `C4` | 解码必须落在守卫里，且 miss 时**回退**而不是抛 |
| `C5` | 共享入口 `OffloadWireBase64.decode` 永不抛，坏载荷回 `null` |

## 2. 轴

四条见证，三种轴：

- **dataflow**（C1、C5）：构造了对的解码器 ≠ 用它解码；`runCatching` 里 `.getOrThrow()` 等同于没有守卫。
- **spelling**（C2）：判据锚「行里含 NO_WRAP 且不含 DEFAULT」——`NO_WRAP or URL_SAFE` 两个子串都满足，语义相反。
- **dataflow / 失败路径**（C4）：共享入口的 `null` 是**值**，调用点把它变成 `throw` 就等于把回退契约翻成中止契约。

## 3. 见证（逐字）

### 见证 4 · C1
`ProviderExecutionGateway.kt:194`
```
    data = runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()
→   data = runCatching { java.util.Base64.getMimeDecoder(); ByteArray(0) }.getOrNull()
```
旧门为什么绿：只问 `MIME_DECODER` 这个**字符串**是否出现在 gateway 里。
真实后果：`data` 恒为零字节数组——生成的图/音频全部落成 0 字节文件，attachments 面板显示空白缩略图，而 take 标记、`LLMMediaAttachment` 全都在。

### 见证 5 · C2
`ModelExecutionService.kt:1092`（media anchor 行）
```
    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))
→   put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP or android.util.Base64.URL_SAFE))
```
旧门为什么绿：`"Base64.DEFAULT" in ln` 为假、`"NO_WRAP" in ln` 为真。
真实后果：字母表被换成 `-`/`_`，消费侧 MIME 解码器**静默丢弃**这两个字符（MIME 解码器忽略非字母表字符）——不是报错，是每个生成图/音频都少几个字节位、整体错位。最坏形态是「看起来能打开但内容损坏」。

### 见证 6 · C5
`OffloadWireBase64.kt:46`
```
    else runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()
→   else runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrThrow()
```
旧门为什么绿：判据是两个子串 `getMimeDecoder().decode(` 与 `runCatching`。
真实后果：这个对象存在的**唯一理由**就是「坏载荷回 null，别抛」，`getOrThrow()` 把它翻回抛出——一个截断的块能从 chunk reader 一路炸穿到每个调用方的协程。

### 见证 7 · C4
`ChatStreamJsonl.kt:138`
```
    data = OffloadWireBase64.decode(b64) ?: ByteArray(0),
→   data = (OffloadWireBase64.decode(b64) ?: throw IllegalArgumentException("bad b64")),
```
旧门为什么绿：只查 `OffloadWireBase64.decode(` 在文件里，且该解码「在守卫里」。
真实后果：一条空/截断的 b64 chunk 直接终止整条流读取——用户看到的是「回复到一半停住」，而正确的语义是丢这一块继续。

## 4. 修法

新增四个解析辅助：`find_close()`（括号配平，配不上即 -1 → 报红）、`split_args()`（顶层实参切分）、`flag_tokens()`（标志表达式 → 词元集合）、`statement_tail()`（语句尾：顶层 `,`/`;`/换行或收尾括号）。

| 判据 | 旧 | 新 |
|---|---|---|
| C1 | `MIME_DECODER in gw_code` | 每个 `getMimeDecoder()` 必须**紧跟** `.decode(`（构造即消费）；再取解码调用的闭合位置，检查它与所属 `runCatching`/`try` 块的结束之间**只有空白**——`; ByteArray(0)` 这种「解了再丢」判红 |
| C2 | 逐行子串 `NO_WRAP` / `DEFAULT` | 从 masked 视图取每个 `Base64.encodeToString(` 的实参，最后一个实参按 `or` 切成词元、去掉 `android.util.Base64.` 限定，**集合必须恰好 `{NO_WRAP}`**；变量/其他修饰词一律红（fail-closed） |
| C4 | 解码须在守卫内 | 追加：JSONL 里每个 `OffloadWireBase64.decode(` 的语句尾不得出现 `throw` / `!!`（miss 必须是回退） |
| C5 | 两个子串在 | 追加：不得出现 `getOrThrow()` / `!!` / `.get()`，且必须由 `getOrNull()` 收尾（fail-closed） |

selftest 新增 5 条臂（4 条见证原样 + 1 条「解了再丢」等价形态），收尾标记 `OFFLOAD_MEDIA_B64_SELFTEST=PASS` 原样保留。

## 5. 四层证据

**L1**
- 真树：`python3 scripts/scan/offload_media_b64_wire_check.py .` → rc=0（`OK (2 media encode sites NO_WRAP, consumer MIME decoder)`）
- selftest：`--selftest` → 18 条臂全 PASS，`OFFLOAD_MEDIA_B64_SELFTEST=PASS`，rc=0

**L2 见证反攻**（旧门 rc / 新门 rc / restore）

| 见证 | old rc | new rc | restore |
|---|---|---|---|
| C1 gateway MIME decoder not consumed | 0 | 1 | True |
| C2 worker `NO_WRAP or URL_SAFE` | 0 | 1 | True |
| C5 shared entry `getOrThrow()` | 0 | 1 | True |
| C4 chunk reader throws on a miss | 0 | 1 | True |

**L3 邻门**：`offload_relative_path_unify_check` / `offload_cli_args_check`（+ `offload_host_path_check`）rc=0

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

1. **合法等价形态被判红**：`val dec = java.util.Base64.getMimeDecoder(); val bytes = dec.decode(b64)`
   - 触发条件：有人把解码器提取成局部变量（当前树是直链，无此形态）
   - 触发后方案：C1 的链路判据扩「绑定到 `val` 后 `X.decode(` 一跳」
   - 搁置理由：fail-closed 方向安全（红 = 有人来看），且本波零生产代码改动、无需为此预建解析分支。
2. **`NO_PADDING` 也被判红**
   - 触发条件：有人给线上标志加 `NO_PADDING`（语义上 MIME 解码器可容忍无 padding）
   - 触发后方案：允许集从 `{NO_WRAP}` 扩到 `{NO_WRAP, NO_PADDING}`，同时补一条「不允许 URL_SAFE / URL_SAFE_NO_PADDING / CRLF」的黑名单与负向臂
   - 搁置理由：现在扩等于放宽到一个真实代码里不存在的写法；且方向是「先红后议」，不会静默放行。
3. **C2 只审对 `ModelExecutionService` 的 `encodeToString` 站点**（未覆盖 `ChatStreamJsonl.kt` 里 `getEncoder()`）
   - 触发条件：JSONL 侧把 `getEncoder()` 换成 MIME/flags 并上 wire
   - 触发后方案：把 producer 判据换成「所有跨进程 base64 生产者」派生集合（walk 全部 `encodeToString`/`getEncoder` 站点）
   - 搁置理由：JSONL 的 b64 是 worker→gateway 的 chunk 通道，走同一 decode 入口，C3/C4/C5 已覆盖消费侧；扩生产者面属另一条边。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-04-offload
python3 probe/l2_verify.py            # 11/11 CAUGHT（本支三门合并）
python3 scripts/scan/offload_media_b64_wire_check.py . --selftest
python3 scripts/scan/offload_media_b64_wire_check.py .
```

> `probe/` 未入库（`.gitignore` 覆盖）；`l2_verify.py` 留在本机同名 worktree 的 `probe/` 下，等价手写复现 = 「打补丁 → `git show HEAD:scripts/scan/<门>.py > /tmp/old.py` 跑旧门（期望 rc=0）→ 跑新门（期望 rc≠0）→ `git checkout -- <file>` + `sha256sum` 核对」。
