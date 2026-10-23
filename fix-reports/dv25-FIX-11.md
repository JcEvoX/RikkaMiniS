# dv25-FIX-11 · B-07（P1）SAF 读无上限：两个导入屏把用户选中文件整个读进堆

> 本报告由主会话收口时按 `backlog §1045-B07` + 分支提交 + CI 判读重建（支 11 会话未落盘报告文件）。

## 1 身份链

- 基线 main `1d44ec90`；分支 `fix/dv25-b7-saf-bounded-read`，4 提交（`47780589` 生产 / `74902651` 测试 / `4956fbe0` 门 / `17bb3f22` 注释可扫描性修正）
- 5 文件 **+876/−19**（生产 3 含 1 新增 / 测试 1 新增 / 门 1 扩）；门 109 / 夹具 336 不变（0 新建门）
- CI run `39916372226`（#2538 / job `120290726737`）success，`head_sha=17bb3f22` 三方逐字符一致（EXACT）
- 前置 run #2536 绿于 `4956fbe0`，因注释改动重推重跑（不拿 SRC-EQ 顶账）

## 2 缺陷本体

- `ProviderListScreen:233` 非 zip 分支 `openInputStream(uri)?.bufferedReader()?.use { it.readText() }` —— **零上限**（同一 launcher 的 `.zip` 分支有 `ProviderImportZip` 2 MB/16 MB 双守卫）。
- `SkillsManagementScreen.readUriBytes:448` 裸 `use { it.readBytes() }` —— **零上限**；`SkillRepository.readZipEntries` 的 `maxExpanded = 32 MiB` 是**解压后累计**上限，对已进堆的原始数组帮不上忙（`isZipArchiveBytes` 嗅探发生在分配之后）。
- ⇒ 用户可选任意大小 `.json` / `.zip` 直接 `OutOfMemoryError`。报告称 `SkillArchiveExtractor.kt` 与 `MAX_SKILL_MD_BYTES/MAX_TOTAL_ARCHIVE_BYTES` —— **真树不存在**（见 §6）。

## 3 修法（阶梯第 5 级：复用既有上限工具，不新造方言）

新增 `ui/settings/SafImportRead.kt`（同包 `internal`，不新造公共 API 包）：`readSafBytes(resolver, uri, cap)` 单一有界入口 + 纯核 `readAtMost(input, cap)`；**上限双重强制**（`OpenableColumns.SIZE` 只作廉价早退、**绝不单独信任**；流读读到 cap+1 停才是权威）+ `SafRead.Ok/TooLarge/Failed` 三态；**恰好 cap 字节必须整篇接收**（多读那 1 字节是 overflow probe；写成 `while (total < cap)` 会把合法文件静默截断成 cap−1）。上限对齐：provider JSON 16 MiB = `ProviderImportZip.MAX_TOTAL_BYTES`；技能 32 MiB = `readZipEntries.maxExpanded`。`readUriBytes` 删除（留一个「无上限读」的壳 = 留陷阱），两个调用方同批改完，全仓引用 0。

## 4 门（扩既有 `filepreview_print_bounded_check.py`，0 新建门）

主体集合**扫出来不写死**：遍历 `ui/**` 每处 `openInputStream(`，站点必须归入四类有界形态之一（streaming copy / bounded helper / declared-size precheck / capped extractor），否则违规或带理由登记豁免；真树 5 站点全归类（4 有界 + 1 豁免）。另加：两个消费屏禁直调 `openInputStream`、必须调 `readSafBytes`、必须出现 `TooLarge`、必须显式传 `MAX_*_BYTES`；helper 侧 `readAtMost` 唯一 + 体内有「字节数 vs cap」比较 + 用 `return null` 报溢出；**豁免腐坏 = ROT 红**。8 个新臂逐臂精确红。

## 5 验证四层

- L1 本门真树 rc=0 + `--selftest` PASS（12 旧臂 + 8 新臂）+ **PRE 臂基线树 rc=1**（17 违规 + 1 ROT，含 `ProviderListScreen.kt:233` 与 `SkillsManagementScreen.kt:429` 两条缺陷本体行）
- L2 概念反查 6 道邻门 6/6 rc=0
- L3 真 kotlinc + JUnitCore 7 臂：POST `OK (13 tests)` / **PRE 精确 3 红** / m1 2 红 / m2 3 红 / m3 1 红 / m4 2 红 / m5 1 红 —— 6 变异臂全精确
- L4 CI #2538：26 步 `non-success=[]`、唯一 skipped=Publish、`109 passed, 0 failed` 恰一条、门头 1..109 连续互异、`ALL 336`、`Z e: ` 0、游离 FAILED 0、`SafBoundedReadTest` 13/13 PASSED（`probe/ci/judge.py` 17/17）

## 6 报告失真

报告引用的 `SkillArchiveExtractor.kt` 与 `MAX_SKILL_MD_BYTES=512KB` / `MAX_TOTAL_ARCHIVE_BYTES=20MB` **在真树不存在**；真正起作用的下游守卫是 `SkillRepository.readZipEntries` 的 `maxExpanded = 32 MiB`，且它只管解压阶段 ⇒ 缺陷本体比报告所述更强（上游全量 `readBytes` 完全无界）。

## 7 两条规则级收获

1. **门遇到「主体文件缺失」不得提前 return**：首版在 `SafImportRead.kt` 不存在时短路，基线树只报「helper 不见了」而不报缺陷本体 ⇒ 门报了症状、藏了病。缺文件只记 ROT，sweep 继续跑完。
2. **注释改动也会推走 HEAD**：`ponytail:` 多行 KDoc 改单行（`scan_debt.py code` 只认注释行上的标记）导致必须重推重跑；`SRC-EQ` 只适用于 `fix-reports/` 这类不入构建的路径。

## 8 残留

R1 真机行为级（>16 MiB json / >32 MiB zip 不 OOM、提示 too large）· R2 超限文案是英文字面量未进 strings.xml（P2，COMMON 硬约束禁动 8 语言文件）· R3 `ChatPromptAndTools.kt:1629` 图片附件读仍无上限（P2 latent，已登记豁免）· R4 技能 zip 仍「先全量进堆再解压」（P2，有 32 MiB 硬上限）· R5 两道技能上限有意并存（登记备查）· R6 `readAtMost` 对谎报小尺寸只能靠流读兜底（P3）· R7 门的有界形态判据锚拼写（P3，方向多报）· R8 门作用域限 `ui/**`（有意）。全带触发条件。

## 9 影响面

两个导入屏的 SAF 读不再无界；超大文件现在是**可见的拒绝**而不是 OOM 崩溃。未改：`ProviderImportZip` 管线、`readZipEntries` 的 32 MiB 守卫、`strings.xml`/7+1 语言、schema/依赖。
