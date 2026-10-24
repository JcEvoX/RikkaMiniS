# FIX-META-WIDE — skill_frontmatter_roundtrip_check（wide-1024）

分支：`fix/meta-wide-10-skill-backup-compact` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-10-skill-backup-compact`
基线 sha：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
生产代码改动：**0 行**（只改本门脚本）

## 1. 门

`scripts/scan/skill_frontmatter_roundtrip_check.py`（技能 frontmatter 从解析 → 合并 → 落盘 → 三处 UI 的全链路门，17 arms）。

## 2. 轴

| 见证 | 轴 | 旧判据为何瞎 |
|---|---|---|
| arm1 函数体极性反转 | dataflow | 判据把「极性」锚在**形参顺序**上（第一个形参名含 new、第二个含 disk），再检查函数体里有 `ifBlank` 与 `?:` 两个子串。把 `?:` 两侧操作数对调，形参与子串全都不变 |
| arm7 列表预置分隔符 | dataflow | 判据锚 `for (i in 1 until frontmatterEnd)` 这个**下界**，只证明跳过源文档的开头分隔符；把累加器预置成 `mutableListOf("---")` 则另造一个分隔符 |
| arm10 候选集常量化 | dataflow | 判据只验证「某条 `val x = resolveUpdateTargetId(` 在导入之前、且结果被带下去」，不验证**第一个实参**是不是活的候选集 |

三条同病：判据锚「某个 token 存在」，没锚「那个值是否承重」。

## 3. 见证（外部审计原样）

1. `SkillRepository.kt`：`): String? = newFrontmatter?.ifBlank { null } ?: diskContent` → `diskContent?.ifBlank { null } ?: newFrontmatter`
   —— 磁盘旧块永远压过新块，通过 URL/文件更新技能时新增的依赖 / 版本 / 子代理段被整体丢弃（ISSUE-05 头号症状）。
2. `SkillRepository.kt`：`val fm = mutableListOf<String>()` → `val fm = mutableListOf("---")`
   —— 每次保存 SKILL.md 输出开头变成两行 `---`，下次启动整个 frontmatter 解析失败。
3. `MinisSkillsBrowserScreen.kt`：`resolveUpdateTargetId(skillRepository.skills.value.map { it.id to it.name }, …)` → `resolveUpdateTargetId(emptyList(), …)`
   —— 去重永远返回 null，更新已安装技能时走新建分支（重复安装 / 覆盖失败的静默版本）。

修复前逐条打补丁实测：**rc=0（门瞎）**。

## 4. 修法

- **arm1 → `check_resolve_polarity()`**：从**函数体**派生判据。解析形参表取真实形参名，再取表达式体（支持跨行、跨 `{ null }` lambda 的语句切分），找到**顶层** `?:`，要求「第一个形参出现在左侧、第二个出现在右侧」。无 elvis / 块体 / 括号失衡 → fail-closed 违规并在消息里写明 re-point 提示。形参改名不影响（名字从签名派生）。
- **arm7 → `check_merge_seed()`**：从 `1 until frontmatterEnd` 循环体内派生**累加器变量**，取它的 `val` 初始化表达式（跨行链式安全），要求初始化器里不含 `---` 字面量、且循环前没有 `+= "---"` / `.add("---")`；另外要求函数体里至少发出一次开头 `---`（否则文档没有 frontmatter 分隔符）。
- **arm10 → `resolve_target_first_arg_problems()`**：对**每一个** `resolveUpdateTargetId(` 调用点（跳过声明本身）解析实参表，检查 arg0 是否为 `emptyList()` / `null` / 空参 / 纯字面量集合。不可解析 → fail-closed。

## 5. 四层证据

- **L1**：`python3 scripts/scan/skill_frontmatter_roundtrip_check.py .` → rc=0，`… --selftest` → `SKILL_FRONTMATTER_ROUNDTRIP_SELFTEST=PASS`（13 组夹具 + 新增 3 组，全部按预期）。
- **L2 反攻**：B1/B2/B3 三条见证 → 修复后 rc=1、`restore=True`（sha256 逐字节一致）。修复前同补丁 rc=0。
- **L3 邻门**：`skill_restore_session_cleanup_check` rc=0、`compact_entry_pin_migration_check` rc=0。
- **L4 全量**：`GATE_RUN mode=full ran=112 pass=112 fail=0 sandbox=0 verdict=GREEN`；`test_scan.py` → `✅ ALL 344 CASES PASS`。

自证臂（断言**恰好红对应判据**，selftest 循环支持 needle 断言）：
- `DIRTY_SR_POLARITY_BODY`（形参顺序正确、体反）→ 必须命中 `does not GUARD the fresh block`
- `DIRTY_SR_SEED_DELIM`（`mutableListOf("---")`）→ 必须命中 `seeds \`fm\` with a delimiter literal`
- `DIRTY_SB_EMPTY_TARGETS`（`emptyList()`）→ 必须命中 `CONSTANT candidate set`

原有「攻击被挡住」臂（arm7 spelling 变体）未动、仍红。

## 6. CI

见回报行（分支 CI 绿后回填 run id / head_sha）。

## 7. 残留（三件套）

- **R1 块体 / `when` 形态的合并基底**：arm1 只认表达式体里的顶层 elvis；若有人把 `resolveMergeBase` 改成块体或 `when`，门会 **fail-closed 红**（方向安全，不是静默绿）。
  触发条件：真树出现该重构且 CI 因此变红。触发后方案：为块体派生 `return <expr>` 并复用同一极性判据。搁置理由：当前形态稳定，提前支持等于为一个没有出现过的写法付装置成本。
- **R2 累加器的跨函数委托**：若把 `fm.add(...)` 搬进同文件 helper，arm7 的累加器派生会失败 → fail-closed 红。
  触发条件：出现该重构且门红。触发后方案：允许一跳同文件委托（沿用 1031-wave R11 的 `countInto()` 处理法）。搁置理由：现无实例。
- **R3 arm10 只判「不是常量集合」**：`instanceEntries.filter { false }` 这类「有派生调用但结果恒空」的写法仍绿。
  触发条件：出现常量折叠式过滤。触发后方案：把「派生调用」判据升级为「派生调用的谓词不含常量 false」。搁置理由：需要轻量表达式求值，超出本支装置比例。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-10-skill-backup-compact
    python3 scripts/scan/skill_frontmatter_roundtrip_check.py .            # rc=0
    python3 scripts/scan/skill_frontmatter_roundtrip_check.py --selftest   # ...SELFTEST=PASS
    python3 /var/minis/shared/work/meta-wide/probe-10/run.py . B1 B2 B3    # 三条 rc=1 restore=True
