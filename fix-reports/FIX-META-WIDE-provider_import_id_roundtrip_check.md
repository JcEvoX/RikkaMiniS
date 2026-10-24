# FIX-META-WIDE-provider-import-id-roundtrip — 名字还在，值没流（导入/发布链）

> wide-1024 宽扫第一波 · 支 01（provider-heavy）· 基线 `564c8f3e` · 分支 `fix/meta-wide-01-provider-heavy`
> 生产代码 0 行改动；`scan.sh` / `test_scan.py` 冻结面 0 触碰。

## 1. 门

`scripts/scan/provider_import_id_roundtrip_check.py`（改写前 1808 行 → 改写后 2071 行，13 条 arm）。

这道门守的是 **`ProviderRepository` 的 id 往返契约与「发布 = 持久化」契约**。本支涉及四条 arm：

| arm | 它声称保证的事 |
|---|---|
| 1 | 导入合并时，新增分支返回的是**重查到的规范 id**（不是内存里那个临时 uuid） |
| 6 | 合并同 `(type,label)` 实例时，解出的实例配置字段真的**写进**了被持久化的对象 |
| 8 | `saveConfig` 发布到运行态的对象与持久化/mirror 的对象是同一个，且它是**规范化后**的那个 |
| 12 | 思考规则先落 DAO 再发布，且发布只可能发生在**成功路径**上 |

冻结断言：`PROVIDER_IMPORT_ID_ROUNDTRIP_SELFTEST=PASS`（未改一个字符）。

## 2. 轴

| 见证 | 轴 | 旧判据锚在哪 | 为什么瞎 |
|---|---|---|---|
| Arm 1 | **dataflow** | 窗口里出现 `firstOrNull` **或** `compositeEntryKey(`，且全函数无裸 `entry.id` | 锚「某子串在窗口里」：重查可以照跑、`compositeEntryKey` 照在，而**返回的值**改从被导入对象上读（`ModelEntry.id` 就是 `uuid`，换个拼写把临时 uuid 带回来） |
| Arm 6 | **dataflow** | `readImportInstanceFields` 与 `applyImportInstanceFields` 两个名字都在 `mergeImportInstanceJSON` 体内 | 锚「调用了正确 API」：算完可以不用 —— 传 `existing` 给 `updateInstance`，全部字段静默丢弃 |
| Arm 8 | **dataflow** | `_config.value = X` 与 `persistToDbAndMirror(X)` 是同一个标识符 | 锚「同一个名字」：`published` 可以从**原始入参**重新派生，于是规范化白跑，名字仍一致 |
| Arm 12 | **order**（缺一条「只允许成功路径」） | `var` 的赋值在 DAO 写之后 | 锚「顺序」：`.onFailure { … }` 里的赋值**也在** DAO 写之后 —— 顺序对了，值却来自失败路径 |

## 3. 见证（外部对抗审计席位产出，本机机械复证）

| # | 文件 / 站点 | 补丁实质 | 缺陷后果 |
|---|---|---|---|
| W5 | `ProviderRepository.mergeImportInstanceJSON` | `?.id` → `?.let { entry.uuid }` | 新增条目返回临时 uuid（`compositeEntryKey` 只在 elvis 回落分支）；`entryIdMap` 带出去的是磁盘上不存在的 id ⇒ 导入的图片/插件引用在重启后指空 |
| W6 | 同上 | `updateInstance(updated)` → `updateInstance(existing)` | 同 `(type,label)` 恢复时 `customBaseURL` / `appendV1Suffix` / `useResponsesAPI` / `customUserAgent` + 五个 run-config 字段全部**解码后被丢弃** |
| W8 | `saveConfig` | `val published = canonical.copy(…)` → `config.copy(…)` | 规范化（`toSnapshot().toProviderConfig()`）真跑了但结果被丢掉 ⇒ 运行态、DB、mirror 三处都是未规范化的旧 id 形状，重启后复合 id 与运行态不一致 |
| W12 | `saveThinkingRule` | `.onFailure { … }` 里加 `persistedRules = listOf(rule)` | DAO 写失败时仍把「已持久化」的规则集发布给 resolver ⇒ 内存/磁盘分裂，重启后规则消失（而 UI 当次显示已保存） |

## 4. 修法（旧判据 → 新判据）

### 4.1 Arm 1：返回的 id 必须**投影自重查结果**

新增两个解析原语：`_top_level_find(text, needle)`（零深度、字符串感知）与 `_statement(text, start)`。

判据：

1. 取 `addEntry(<x>)` 之后到 `entryMap[` 之间的值表达式，按**零深度** `?:` 切成左操作数；
2. 左操作数必须含一次重查（`firstOrNull {` / `.find {` / `firstOrNull(` / `singleOrNull`）；
3. 找出重查 lambda 的**投影段**（lambda 闭合之后的部分）—— 谓词 lambda 合法地会读 `entry.baseModel.id`（它就是在匹配），只有投影段不许读被导入对象；
4. 投影段必须落在 `?.id` / `?.let { it.id }` / `?.key` 这类「读重查结果」的形态上，否则 fail-closed；
5. 无 `?:` 的形态（直接 `compositeEntryKey(…)`，确定性派生）单列一条：允许，但禁止从中读 `entry.uuid` / `entry.id`。

W5 的 `?.let { entry.uuid }` 在**投影段**里出现 `entry.` ⇒ 红；真树 `?.id` ⇒ 绿。

### 4.2 Arm 6：`updateInstance` 的实参必须是 `applyImportInstanceFields` 的产物

~~~python
enriched = {name | `val|var name = applyImportInstanceFields(`}      # 从树派生，不写死 updated
writes   = re.findall(r"\bupdateInstance\s*\(\s*([A-Za-z_]\w*)\s*\)", code)
~~~

判定：`enriched` 为空 ⇒ 红（结果被丢弃 / fail-closed）；没有 `updateInstance(<ident>)` 写回 ⇒ 红（fail-closed）；任一写入实参不在 `enriched` 里 ⇒ 红并列出期望集合。

W6 的 `updateInstance(existing)` 命中第三条。

### 4.3 Arm 8：`published` 必须派生自 `canonical`

~~~python
canon = {name | `val name = … toSnapshot( …`.toProviderConfig …}     # 从 toSnapshot 站点回溯最近的 val 声明
expr  = _statement(code, <val published = 之后>)                      # 跨行、括号感知
root  = expr 的首个标识符
~~~

判定：找不到规范化声明 ⇒ 红（fail-closed）；`published` 无声明 ⇒ 红（fail-closed）；`root` 不在 `canon` 里 ⇒ 红，并在消息里给出「派生自谁 / 期望谁」。

W8 的 `config.copy(...)` ⇒ `root = config` ∉ {`canonical`} ⇒ 红；真树 `canonical.copy(…)` ⇒ 绿。

### 4.4 Arm 12：赋值只允许出现在**成功路径**

新增 `_failure_regions(code)` / `_failure_region_at(code, pos)`：解析 `.onFailure { … }` / `.getOrElse { … }` / `.recover{…}` / `recoverCatching` / `catch (…) { … }` 的**块体区间**（括号/字符串感知）。

在既有「赋值在 DAO 写之后」之外追加两条：

1. `var` 的声明初值必须是 `null`（否则「写失败 ⇒ 回读 DB」的回退没有值可回读，fail-closed）；
2. 任何赋值落在失败区间内 ⇒ 红，消息写明是哪个 handler。

W12 命中第 2 条：`persistedRules = listOf(rule)` 在 `.onFailure` 体内。

### 4.5 未触碰

- 生产代码零改动；冻结面零触碰；`selftest` 收尾标记原样。
- `build_fixture_tree(...)` 新增一个**可选**关键字参数 `save_src=None`（默认行为与原来逐字节一致），只为让 Arm 8 的见证能替换 saveConfig 夹具；非 selftest 代码路径不受影响。

## 5. 四层证据

### 5.1 L1 本门 + 真树 + 自证臂

~~~
$ python3 scripts/scan/provider_import_id_roundtrip_check.py .
PROVIDER_IMPORT_ID_ROUNDTRIP: clean (13 arms)      rc=0

$ python3 scripts/scan/provider_import_id_roundtrip_check.py --selftest
selftest: compliant green / violating red arms 1-6 / … / thinking-assign-before+thinking-no-dao red arm 12 / …
PROVIDER_IMPORT_ID_ROUNDTRIP_SELFTEST=PASS
~~~

新增 4 条见证夹具（每条断言**恰好**红对应 arm，不多不少）：

| 夹具 | 构造 | 期望 |
|---|---|---|
| `FIX_MERGE_ID_OFF_IMPORTED_BAD` | 返回 `?.let { entry.uuid }` | `['1']` |
| `FIX_MERGE_WRITES_UNMERGED_BAD` | `updateInstance(existing)` | `['6']` |
| `FIX_SAVE_PUBLISHED_FROM_RAW_BAD` | `val published = config.copy(` | `['8']` |
| `FIX_THINKING_ASSIGN_IN_FAILURE_BAD` | `.onFailure` 内赋值 | `['12']` |

「恰好」这条不是修辞：`_arm_set(tmp)` 与期望列表求等，多红一条也判 selftest 失败（实证：Arm 1 首版就因未处理「无 elvis 直返 `compositeEntryKey`」的合法形态而让 `merge-whitelist` 夹具多红一条 arm 1，当场暴露并修掉）。

### 5.2 L2 见证反攻

~~~
== provider_import_id_roundtrip_check ==
  Arm 1    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  Arm 6    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  Arm 8    before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True
  Arm 12   before_rc=0  after_rc=1  GATE_CAUGHT_WITNESS  restore=True  [arm 12] ProviderRepository.kt:1056
~~~

命中文本：

~~~
[arm 1] mergeImportInstanceJSON's re-query result is not what it returns:
        it is read off the imported entry `entry` itself (a temp uuid / field that
        saveConfig canonicalized away)
[arm 6] updateInstance(existing) writes a value that did NOT come from
        applyImportInstanceFields (expected one of updated) …
[arm 8] published is derived from 'config.copy(', not from the canonicalized value
        (canonical) …
[arm 12] saveThinkingRule assigns persistedRules inside failure handler
        `.onFailure {` — that body runs only when the write FAILED …
~~~

`before_rc` 取自 `git show 564c8f3e:scripts/scan/<gate>.py` 落到 `probe/baseline/` 的原文件，与修复后同补丁同树对拍 ⇒ 判据链闭合（同一把刀，前瞎后抓），四次 restore 全部 sha256 一致。

### 5.3 L3 邻门

| 邻门 | rc |
|---|---|
| `provider_remember_key_check` | 0 |
| `models_provider_field_check` | 0 |
| `session_provider_enabled_check` | 0 |

### 5.4 L4 全量

~~~
$ bash scripts/scan/scan.sh
║  Summary: 112 passed, 0 failed

$ python3 probe/chunk_driver.py        # 单进程版撞 S28，改为「一函数一进程」
前半 35 chunk: pass=241 fail=0
后半 11 chunk: pass=103 fail=0
总计           344 pass / 0 fail
~~~

（S28 = 沙箱缺陷：单解释器累计约 220 个子进程后 `os.waitpid` 报 `ENOSYS`；`probe/baseline/` 的原始门在同树上同样复现，与本支改动无关。分块跑的总数与冻结阈值 `ALL 344 CASES PASS` 逐数一致。）

## 6. CI run

| 项 | 值 |
|---|---|
| run | `40136161820`（#2638） |
| job | `120998010635` |
| conclusion | **success**，attempt **1** |
| head_sha | `794ca82e7cf0186604a1ebe7676626aeb9574dea` —— 与本地 HEAD **逐字符一致** |
| 触发 | 手动 dispatch（分支 push 不触发 `build-apk.yml`，只认 main） |
| 26 步 | 全部 success；唯一 skipped = **Publish to Releases**（`if: refs/heads/main` 门控，预期） |
| 门步骤 | `Run pre-build scan gate` success |

job 日志逐条判读（`probe/ci/job.log`，1.48 MB）：

- `║  Summary: 112 passed, 0 failed` —— **恰 1 条**（其余 7 条 `Summary:` 是子套件：28/5/14/36/10/22/10 passed，全部 0 failed）；
- `ALL 344 CASES PASS` —— 恰 1 条；
- ` e: ` 编译错误行 —— **0 条**；
- `PROVIDER_IMPORT_ID_ROUNDTRIP_SELFTEST=PASS` / 本门自证标记 —— 在册（新增的 4 条 fixture 臂随 `--selftest` 一起被 CI 见证）。

报告提交发生在 CI 绿之后，源码树与绿 sha 等价：

~~~
$ git diff --name-only 794ca82e HEAD -- . ':(exclude)fix-reports' | wc -l
0
~~~

## 7. 残留（供主会话统一登记，本支未写 backlog / credibility）

| # | 残留 | 触发条件 | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| R1 | Arm 1 只认「值表达式里第一个零深度 `?:`」；若有人写成 `if (hit != null) hit.id else compositeEntryKey(…)` | 真树改写成分支语句形态 | 换成「按语句形态派生：if/else 两支各自求值来源」，同第一波 `_body_opener` 手法 | 真树只有 elvis 形态；两种形态同时支持要先有分支解析器 |
| R2 | Arm 6 的 `updateInstance(<单个标识符>)` 只吃单标识符实参；`updateInstance(applied.copy(…))` 之类会 fail-closed 红 | 有人给写入加链式修饰 | 用 `_argument_expression` 取实参表达式，判定「表达式含 enriched 名字且不是被丢弃的位置」 | 现在是单标识符，直接上表达式判定会把「读 enriched 却传 derivative」判绿，需要更细的数据流 |
| R3 | Arm 8 的 `canon` 由「`toSnapshot(` 站点回溯最近的 `val`」派生；若规范化拆成两个变量（`val snap = config.toSnapshot(); val canonical = snap.toProviderConfig()`）会只认后一个 | 真树把两步拆开 | 沿赋值链递归（`canonical ← snap ← config`），判「某跳含 toProviderConfig」 | 现在是一行链式写法；递归派生要引入变量图，本轮半径外 |
| R4 | Arm 12 的失败区间只覆盖 `.onFailure/.getOrElse/.recover/.recoverCatching/catch`；`Result.fold(onSuccess, onFailure)` 的 onFailure lambda 不认 | 真树把 runCatching 改成 `fold` | 把 `fold { }` 的第二个 lambda 也算进失败区间 | 现在没有 `fold` 用法；误判方向是绿（漏判），已在本次见证之外的形态，等出现再补 |
| R5 | `_failure_regions` 取「调用后第一个 `{`」；`onFailure(logThenRebuild { … })` 取到内层 lambda 的区间会偏窄 | 出现嵌套 lambda 的失败回调 | 用括号配平先跨过实参列表再取块体 | 真树无此形态；偏窄只可能导致漏判（方向已知，登记不动手） |

## 8. 复现命令

~~~bash
cd /var/minis/shared/work/meta-wide/wt-01-provider-heavy

python3 scripts/scan/provider_import_id_roundtrip_check.py .
python3 scripts/scan/provider_import_id_roundtrip_check.py --selftest
python3 probe/run_witness.py provider_import_id_roundtrip_check

for g in provider_remember_key_check models_provider_field_check session_provider_enabled_check; do
  python3 scripts/scan/$g.py . >/dev/null; echo "$g rc=$?"; done

bash scripts/scan/scan.sh | tail -3
python3 probe/chunk_driver.py
~~~
