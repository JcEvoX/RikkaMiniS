# FIX-META-WIDE — compact_entry_pin_migration_check（wide-1024）

分支：`fix/meta-wide-10-skill-backup-compact` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-10-skill-backup-compact`
基线 sha：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
生产代码改动：**0 行**（只改本门脚本）

## 1. 门

`scripts/scan/compact_entry_pin_migration_check.py`（压缩/删除入口后的 pin 与三处 per-device 引用收敛门，9 arms）。
本门是「一次删除要收敛到 9 条边」的唯一机械见证点。

## 2. 轴

| 见证 | 轴 | 旧判据为何瞎 |
|---|---|---|
| arm 4 决策助手被掏空 | anchor/dataflow | 只断言三个 `internal fun decide…(` **声明存在**。把函数体换成 `= null` 后声明仍在、9 个调用点仍在，9 条级联全部静默失效 |
| arm 7 applier 分支死守卫 | dataflow | 只匹配 6 个子串（3 个 `decideStaleRefCleanup(<ref>` 调用 + 3 个 `= null` 写入）。写成 `if (false && decideStaleRefCleanup(...))` 时 6 个子串全在 |
| arm 6 待删 id 集常量化 | dataflow | 只匹配 `val staleEntryIds` 声明 + 三个消费者；把右侧换成 `emptyList<String>()`，四个锚点全在，整条级联变成 no-op |

## 3. 见证（外部审计原样）

1. `ProviderRepository.kt`：`decideCompactPinMigration(...)` 体 → `null`
   —— pin 语义迁移永不再发生：`oldKey`→`newKey` 的压缩后引用全部指向已不存在的 key。
2. `ProviderRepository.kt`：`clearStaleSecondaryRefs` 三处 `if (decideStaleRefCleanup(...))` → `if (false && decideStaleRefCleanup(...))`
   —— 删除 provider 后 `lastUsedEntryId` / `voiceInputOverrideEntryId` / `voiceOutputOverrideEntryId` 永久悬空。
3. `ProviderRepository.kt`：`val staleEntryIds = instanceEntries.filter{…}.map{ it.id }` → `val staleEntryIds = emptyList<String>()`
   —— 种子条目仍从 `config.modelEntries` 溶解，但级联被告知「没有条目被删」：pin / group members / agent-loop pins / per-device refs 全部悬空。

修复前逐条打补丁实测：**rc=0（门瞎）**。

## 4. 修法

- **arm 4 → `check_helper_bodies()`**：对每个助手**从签名派生函数体**（表达式体与块体都支持、跨行/跨 lambda），
  判据两条：（a）体不得是常量（`null` / `false` / `true` / `""` / `emptyList()` 等）；
  （b）体必须读到自己声明的每个形参（读不到的形参 = 该决策看不见这个输入）。
  体不可解析 → fail-closed 违规。
- **arm 7 → `check_clear_applier()`**：在 applier 体内**逐条 `if (` 解析条件**（括号配平），条件里出现常量假（`false` / `!true` / `0 != 0` / `1 == 0` / `true == false`）即红；
  另外要求三个引用各自在体内被赋 `null`（光有判定、不执行清理也算失败）。
- **arm 6 → `check_stale_entry_ids()`**：把 `val staleEntryIds` 当**表达式**读（跨行链式 `.filter{…}.map{…}` 正确拼接），
  判据：不得是空/字面量集合；必须含集合派生调用（`.map` / `.filter` / `.mapNotNull` / `.toSet` …）。派不出来 → fail-closed。

## 5. 四层证据

- **L1**：`python3 scripts/scan/compact_entry_pin_migration_check.py .` → rc=0；
  `… --selftest` → `COMPACT_ENTRY_PIN_MIGRATION_SELFTEST=PASS`，既有 16 组夹具全部按预期
  （其中 `helper=constant` 之前会误伤的 `PR_OK` 夹具，已把三个助手体从占位的 `= TODO()` 换成真实决策表达式，
  这正是新 arm 4 要求的形态，夹具因此更贴近真树）。
- **L2 反攻**：D1 / D2 / D3 → 修复后 rc=1、`restore=True`（sha256 一致）；修复前同补丁 rc=0。
  红行分别指向 arm 4 / arm 7 / arm 6，且 `[da1024-C]` 段的行号可核对。
- **L3 邻门**：`backup_streaming_export_check` rc=0、`loop_persist_slice_check` rc=0。
- **L4 全量**：`GATE_RUN mode=full ran=112 pass=112 fail=0 sandbox=0 verdict=GREEN`；`test_scan.py` → `✅ ALL 344 CASES PASS`。

自证臂（`_report(..., contains=…)` 断言命中被判据）：
- `PR_W_BAD_HELPER_CONST` → `returns a CONSTANT`
- `PR_W_BAD_DEAD_GUARD` → `DEAD condition`
- `PR_W_BAD_EMPTY_STALE_IDS` → `is a CONSTANT`

原有「攻击被挡住」臂（arm 6 的 `bare-seed-ids` 变体）未动、仍红。

## 6. CI

见回报行（分支 CI 绿后回填 run id / head_sha）。

## 7. 残留（三件套）

- **R1 常量折叠式助手体**：arm 4 判「非常量 + 每参数被读」，`= if (true) null else newKey` 这类恒空折叠仍绿。
  触发条件：出现该写法且真机复现引用悬空。触发后方案：体级常量折叠（只折叠字面量条件）。搁置理由：需要表达式求值，装置比例不符。
- **R2 派生调用的谓词为常量**：arm 6 只要求「有派生调用」，`instanceEntries.filter { false }.map { it.id }` 仍绿。
  触发条件：出现恒空谓词。触发后方案：派生调用谓词里禁止 `false` 字面量。搁置理由：现无实例。
- **R3 助手迁到别的文件**：arm 4 的 `decl_body` 只在 ProviderRepository 内查找；若助手被抽到单独文件，判据 fail-closed 红。
  触发条件：真树出现该重构且门红。触发后方案：按 `fun decide…(` 名字在整棵 main source set 派生。搁置理由：当前 0 实例，且红是「安全方向」。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-10-skill-backup-compact
    python3 scripts/scan/compact_entry_pin_migration_check.py .            # rc=0
    python3 scripts/scan/compact_entry_pin_migration_check.py --selftest   # ...SELFTEST=PASS
    python3 /var/minis/shared/work/meta-wide/probe-10/run.py . D1 D2 D3    # 三条 rc=1 restore=True
