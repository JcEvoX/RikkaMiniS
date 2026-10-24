# FIX-META-WIDE — skill_restore_session_cleanup_check（wide-1024）

分支：`fix/meta-wide-10-skill-backup-compact` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-10-skill-backup-compact`
基线 sha：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
生产代码改动：**0 行**（只改本门脚本）

## 1. 门

`scripts/scan/skill_restore_session_cleanup_check.py`（restore / promote / recordSkillUse 三条数据链的门）。
本波三条实证见证全部命中本门；本门同时是 `skill_frontmatter_roundtrip_check` 与
`compact_entry_pin_migration_check` 的语义邻门。

## 2. 轴

| 见证 | 轴 | 旧判据为何瞎 |
|---|---|---|
| A1 `preferredId.let { null }` | dataflow | arm 1 只断言「语句里有 resolveImportId 调用」+「语句里出现 preferredId 字符串」。把实参换成一个恒为 null 的表达式，两个断言都成立 |
| A2 recordSkillUse 读/写分离 | scope + dataflow | arm 4 只断言「`_skills.value =` 写在 `synchronized(addLock)` 块内」。把 find/map 与 db.execSQL 挪到锁外、只留 publish 在锁内，判据仍绿 |
| A3 `_inputText.value = String()` | spelling | C2 用 `_inputText\.value\s*=\s*["\']` 认「赋常量」，只认字面量写法；`String()` 是同一行为的另一种拼写 |

三条合起来的共同病：**判据锚「某个 token 出现了」，没锚「那个 token 的值有没有被用起来」**。

## 3. 见证（原样，来自外部对抗审计报告）

1. `SkillRepository.kt`：`resolveImportId(preferredId, slugify(name))` → `resolveImportId(preferredId.let { null }, slugify(name))`
   —— 重命名 / 纯 CJK 技能的 id 解析结果被抹成 null，回落 slugify(name)：要么被 slug 守卫拦下（拿不到升级），要么以新 slug 落库（新来的 subagent / max_turns / allowed_tools 段被旧盘上副本盖掉，ISSUE-05 的原始症状）。
2. `SkillRepository.kt`：`recordSkillUse` 把 `find`/`map`/`db.execSQL` 移到锁外，只把 `_skills.value =` 留在锁内
   —— 两个并发调用都读到 bump 前快照，DB 记 +2、内存只 +1（da1029-C 修掉的丢增量竞态回归）。
3. `ChatSessionEntryExt.kt`：在 `ComposerDraftStore.clearDraft(context, draftId)` 之前插入 `_inputText.value = String()`
   —— 在持久化之前清空内存草稿，中间一旦取消 / DB 失败，草稿文本没有任何副本。

修复前逐条打补丁实测：**rc=0（门瞎）**。

## 4. 修法（每轴对症，均按「输出必须被消费」重写）

- **A1 → 新增 arm 1b `check_resolve_callee_args(src)`**：对文件里**每一个**
  `SkillRestorePolicy.resolveImportId(` 调用点（`add()` 与 `importFromContent()` 两处，按站点派生，
  不写死函数名）做**实参级**判定：arg0 必须提到 `preferredId`，且不得含
  `null` / `false` / `!true` / `true==false` / `""` 这类把实参归约成常量的记号。
  参数表括号失配 → fail-closed 违规。
- **A2 → `check_record_skill_use_lock` 扩到整段 read-modify-write**：除既有的
  `_skills.value =` 必须在锁内外，新增「recordSkillUse 体内**每一处** `_skills.value`
  读取都必须落在同一把锁的块内」与「`db.execSQL(` 必须在块内」。赋值点单独归入既有
  publish 判据，避免一条缺陷报两遍。
- **A3 → `COMPOSER_LITERAL_RE` 改为 `COMPOSER_DISCARD_RE`，锚「丢弃」这个效果类**：
  匹配任何「值不可能依赖 `text`」的右侧——字符串字面量（`mask_keep_offsets` 会把内容抹成空格，
  所以 `""` 与 `"abc"` 同属一类）、`String()`、`String.EMPTY`、`buildString {}`。
  赋值语句到变量（`= text`）不匹配。

## 5. 四层证据

- **L1 本门**：`python3 scripts/scan/skill_restore_session_cleanup_check.py .` → rc=0（真树仍绿）；
  `--selftest` → `SKILL_RESTORE_SESSION_CLEANUP_SELFTEST=PASS`，既有 12 组夹具行为不变
  （`publish-outside-lock` 仍 2 条违规——新读点判据把赋值点排除，没有重复计）。
- **L2 见证反攻**（`probe-10/run.py`，12 条见证统一驱动）：
  A1 rc=0→1 / A2 rc=0→1 / A3 rc=0→1，三次 `restore=True`（sha256 逐字节一致）。
- **L3 邻门**：`skill_frontmatter_roundtrip_check` rc=0、`compact_entry_pin_migration_check` rc=0。
- **L4 全量**：`scan.sh` Summary 112 passed / 0 failed；`test_scan.py` ALL 344 CASES PASS（见 §6）。

自证臂（固化成 selftest 夹具，断言**恰好红对应判据**）：
`BAD_SKILL_NEUTRALISED_ID`（必须命中 "NEUTRALISED"，且不得命中 "does not consult preferredId"）、
`BAD_SKILL_RMW_OUTSIDE_LOCK`（必须命中 "OUTSIDE the"，且不得命中 publish 判据）、
`BAD_PROMOTE_STRING_BLANK`（必须命中 "BEFORE the durable body write"，且**恰好 1 条**）。

## 6. CI

见 §6 回报行（本文件在 CI 绿后回填 run id / conclusion / head_sha）。

## 7. 残留（三件套）

- **R1 变量式清空**：`val blank = ""` … `_inputText.value = blank` 不被识别（需要局部常量传播）。
  触发条件：真机出现「promote 中途失败 → 草稿丢失」类反馈，且源码里出现「先赋给变量再清空」写法。
  触发后方案：在 `check_promote_draft` 里加一跳局部常量绑定收集（`val X = <常量>` → 视 X 为常量）。
  搁置理由：当前真树 0 实例；为单条 latent 建数据流装置超出修复门装置比例。
- **R2 别名读取**：`val s = _skills; s.value.find { … }` 这类经别名的读取不在 arm 4 覆盖面内。
  触发条件：真树出现 `_skills` 的别名绑定。触发后方案：从 `val x = _skills` 派生别名集并入扫描。
  搁置理由：现无实例；别名一旦出现会先撞上其它门（add() 的 publish 判据）。
- **R3 `String.EMPTY` / `buildString {}` 已覆盖，但 `"${x}".takeIf { false }` 之类「结果恒空」的表达式未覆盖**。
  触发条件：出现该类写法。触发后方案：扩成「右侧是否引用 `text`」的反向判据（不引用即丢弃）。
  搁置理由：现无实例，且反向判据误伤风险高于收益。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-10-skill-backup-compact
python3 scripts/scan/skill_restore_session_cleanup_check.py .          # rc=0
python3 scripts/scan/skill_restore_session_cleanup_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe-10/run.py . A1 A2 A3    # 三条 → rc=1 restore=True
```
