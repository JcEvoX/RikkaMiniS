# FIX-META · M2-a — `file_provider_paths_coverage_check.py`（轴：dataflow / 目录集合 + 绑定）

- **分支**：`fix/meta-audit-m2-binding`
- **基线**：main `20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- **工单**：元审计环第一波 · M2-binding（外部对抗审计 12/12 实锤中的第 1 门）
- **停止节点**：分支 CI 绿（§6）。**未合并 main**，生产代码一行未动。

---

## §1 门与轴

| 项 | 值 |
|---|---|
| 门 | `scripts/scan/file_provider_paths_coverage_check.py` |
| 轴 | **dataflow**（目录集合双向 + 属性绑定） |
| 见证文件 | `src/android/app/src/main/java/com/rikkaminis/app/data/storage/MediaStore.kt` |

## §2 见证摘要（外部模型给的，机械复证过）

```diff
-    val mediaBaseDir: File = File(context.filesDir, "media")
+    private val _legacyMediaDir: File = File(context.filesDir, "media")
+    val mediaBaseDir: File = File(context.filesDir, "media-unregistered")
```

门仍 rc=0。缺陷后果：所有聊天附件写进 `filesDir/media-unregistered/`，XML 未声明 ⇒
`FileProvider.getUriForFile` 对其中每个文件抛 `IllegalArgumentException`（外部打开/分享全崩）。

**为什么门是瞎的**：C1/C2 遍历**手写注册表** `WRITER_DIRS`，判据是「`MediaStore.kt` 里**某处**存在
`File(context.filesDir, "media")` 且 XML 声明了 `media/`」。见证把合法字面量留给一个**再也没人读**的
`_legacyMediaDir`，把每条 `saveMedia` 真正用的 `mediaBaseDir` 移走 —— 注册表看见「字面量还在、声明也在」，
判绿。它从不问「**实际被写的那个目录**是哪个」。

## §3 现状判据 → 新判据

### 现状（漏洞两处）

```python
for rid, rel, writer_re, tag, path in WRITER_DIRS:      # ← 手写表是唯一枚举源
    if not re.search(writer_re, wcode): violation(...)   # ← 「某处存在」即算通过
    if (tag, path) not in declared_set: violation(...)
```

### 新判据

**C10（目录集合双向，从树里派生而不是从表里抄）**：扫每个被守卫文件里**全部**
`File(context.filesDir|cacheDir, <arg>)` 站点，判「这是不是一个**目录根**」（绑定到 `val/var/属性`、
紧跟 `.mkdirs()`、或作为 `fun … : File =` / `return` 的返回值），然后把目录名与 XML 声明比对：
`<files-path>` ↔ `filesDir`、`<cache-path>` ↔ `cacheDir`；**凡未声明即红**。
不再只认注册表里那四个名字，也不再有「字面量还在就算数」。字面量支持**常量折叠**（`"media" + "-unregistered"`）。

**C11（绑定）**：注册表里每条目录必须绑到一个**活**的 `val/var/属性`（该标识符在文件里被读 ≥2 次，
即声明之外至少还有一次使用）。见证的 `_legacyMediaDir` 只出现 1 次 ⇒ 死绑定 ⇒ 红。
即使攻击者把 `media-unregistered` 也补进 XML（C10 于是沉默），C11 仍红 —— 见 §5 的 `WITNESS variant` 臂。

**fail-closed 三处**：目录字面量解析不出（模板/变量/非常量表达式）→ 红，不跳过；被守卫文件缺失 → 红；
`File()` 括号不闭合 → 该站点视为无法解析 → 红。

**半径外（刻意不扩）**：句柄型 `File(context.filesDir, "browser_tabs/$sid.json").delete()`
是**文件**路径不是 provider 根（模板 + 非 `mkdirs` + 非绑定），不收；`"config.json"` 这类带扩展名的
函数返回值也不收（见 `dir_root_sites` 的说明）。只收目录根，不把整个 filesDir 变成禁区。

## §4 改了什么（diff 骨架）

```
scripts/scan/file_provider_paths_coverage_check.py | 246 ++++++++++++++++++++
```

- 新增 `FILE_CTOR_RE` / `BASE_TAG` / `ASSIGN_RE_TAIL` / `RETURN_RE_TAIL` / `MKDIRS_RE_HEAD`
- 新增 `_close_paren()`（字符串感知）、`fold_dir_literal()`（常量折叠）、`dir_root_sites()`、`declared_covers()`
- `check()` 里插入 C10 / C11 两段；其余 C3–C9 判据**一字未动**
- **夹具修形（重要）**：原 `CLEAN_MEDIASTORE` 夹具里 `mediaBaseDir` **声明后从未被读** —— 即旧夹具
  本身就是「死绑定」形状，C11 一上来就红。这不是对真树的改动，是**夹具不真实**：真 `MediaStore`
  每处 `saveMedia` 都读它。夹具补成「声明 + 被 `saveMedia` 使用」，C11 才有资格作为契约。

## §5 四层验证

### L1 — 本门真树 + selftest

```
$ python3 scripts/scan/file_provider_paths_coverage_check.py .
Summary: 0 violation(s) found          # rc=0

$ python3 scripts/scan/file_provider_paths_coverage_check.py --selftest
FILE_PROVIDER_PATHS_SELFTEST=PASS (30 arms)   # 旧 25 臂 + 新 5 臂，全 OK
```

新 5 臂（全部带牙，且**旧判据会放过的形态**）：

| 臂 | 期望 | 实测命中 |
|---|---|---|
| `WITNESS dead alias + re-pointed live dir`（见证原文） | 红 | `C10 + C11` |
| `WITNESS variant: new dir declared, only C11 can see it`（把新目录也加进 XML，C10 沉默） | 红 | **仅 `C11`** |
| `dir literal assembled by concatenation`（`"media" + "-unregistered"`） | 红 | `C10 + C11 + C2` |
| `extra undeclared dir root added`（`fun scratch(): File = File(filesDir, "photos-scratch")`） | 红 | `C10` |
| `dir root built from a variable`（`File(filesDir, dirName)`） | 红 | `C10`（fail-closed） |

`CLEAN` 臂仍绿 —— 即新判据不是「一律红」。

### L2 — 机械复证（打见证 → 门必须红）

```
$ python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
      /var/minis/shared/work/meta-audit/probe-m2/reports/report-A.md \
      --repo /var/minis/shared/work/meta-audit/wt-m2
── file_provider_paths_coverage_check.py
   [1] -        rc=1  restore=True  → ❌ 门抓住了（见证无效）
        VIOLATION: C10 […] directory `filesDir/media-unregistered` is created (mediaBaseDir = …
        VIOLATION: C11 [media] the only bindings of `files-path/media` … are dead (['_legacyMediaDir']…
```

`restore=True`（sha256 复原）。修前同一条命令是 `rc=0 → GATE_BLIND_CONFIRMED`（主会话已归档）。

### L3 — 全量门禁 + 全量夹具

```
$ sh /var/minis/shared/tools/gate_run.sh --full --repo … --chunks 4
GATE_RUN mode=full ran=112 pass=112 fail=0 sandbox=0 envretry=0 verdict=GREEN
```
（首跑 `test_scan.py` 红过一条：见 §8 R1 —— 冻结的 marker 契约，非判据问题，已修并重跑）

```
$ python3 scripts/scan/test_scan.py
✅ ALL 344 CASES PASS
```

`scan.sh` 单独跑过：`Summary: 112 passed, 0 failed`。

### L4 — CI（§6）

## §6 CI

| 项 | 值 |
|---|---|
| run | **40102170254**（https://github.com/logicflow-GYW/RikkaMinis/actions/runs/40102170254） |
| job | `120896393210`（build），attempt 1，`conclusion=success` |
| `head_sha` | **`0fcb8505541b3fb0e41218033d9b1ed98bcbefd6`** — 与本地 `git rev-parse HEAD` 逐字符一致 |
| 26 步 | `bad=[]`，唯一 `skipped` = 步骤 23 `Publish to Releases`（main 门控，分支上应跳） |
| 门禁 | 门头 `[1/112] … [112/112]`，恰一条 `Summary: 112 passed, 0 failed`（其余 Summary 行是各门内部小计） |
| 夹具 | 恰一条 `ALL 344 CASES PASS` |
| 编译错 | ` e: ` 计数 **0**，游离 `FAILED` 0 条 |
| 本支两门 | `PASS browser_tab_exec_serial (…)` / `✅ browser_tab_exec_serial_check selftest has teeth` / `✅ … real tree green`；`✅ file_provider_paths_coverage_check selftest has teeth (FILE_PROVIDER_PATHS_SELFTEST=PASS)` / `✅ … real tree green` |
| 日志 | 1,483,218 B（判读脚本：`grep -c` 三条 + jobs API 取步骤结论） |

## §7 残留登记（不动手）

| # | 残留 | 为什么不动 |
|---|---|---|
| R1 | `test_scan.py:2697` 用**收尾 marker 原文**当「门有牙」的证据（`SELFTEST=PASS (browser_tab_exec_serial: 3 fixtures)`）。本支加臂后改了一行汇总文字，立刻红 —— 现已把原文原样保留、臂数另起一行。**冻结面的 marker 是精确子串契约，不是前缀**；将来任何门加臂都必须单独一行报告增量 | 冻结面（`test_scan.py`）本轮禁改；登记为「加臂纪律」 |
| R2 | 目录根识别是**语法启发式**（赋值 / `mkdirs` / 函数返回），不是类型推导：`File(context.filesDir, "x").let { … }` 这类不带 `mkdirs` 又不绑定的形态不会被收 | 收紧会误伤 `browser_tabs/$sid.json` 这类真实文件站点；触发条件 = 真出现「既不被绑定、也不 mkdirs，却当目录用」的写法 |
| R3 | `C1/C2` 注册表仍然保留（与新判据并存）：C10 从树派生，C2 保证注册表本身不腐烂 | 两者职责不同，删 C1/C2 会丢掉「注册表指向的写站点已不存在」这条 |
| R4 | 「被守卫文件」仍是 5 个硬编码 rel（`KT_REL` / `MEDIASTORE_REL` / `PHOTOS_REL` / `CHATVM_REL` / `SCREEN_REL`） | 扩到全 `src/android` 需要先有「哪些文件会产出 provider 路径」的派生规则，属新门范畴；触发条件 = 有第六个 writer 落地 |
| R5 | 常量折叠只支持字符串字面量 `+`；`"$prefix-x"` 模板仍走 fail-closed 红 | 模板求值需要作用域模型；当前真树无此形态 |

## §8 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m2
python3 scripts/scan/file_provider_paths_coverage_check.py .            # rc=0
python3 scripts/scan/file_provider_paths_coverage_check.py --selftest   # 30 arms PASS
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
    /var/minis/shared/work/meta-audit/probe-m2/reports/report-A.md --repo "$PWD"   # 本门 rc=1
```
