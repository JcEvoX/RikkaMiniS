# FIX-A 先证证据（真树复核，基线 d0c1c9219）

## ISSUE-01：importFromContent 新装分支丢弃已解析的 preferredId —— 成立
- `SkillRepository.kt:271` `fun add(`：签名无 `preferredId`；`:286` 首行 `val id = slugify(name)`；`:287` `if (id.isBlank()) return null`。
- `importFromContent`（`:581`）**已有** `preferredId: String? = null` 参数（[fix/audit-b22] 备份恢复路径已传，ConfigBackup.kt:1269 + `s.optString("id", ...)`），且 `:601` 已按 `SkillRestorePolicy.resolveImportId(preferredId, slugify(parsed.name))` 解析出 id —— 但 `:635` 的 `return add(...)` **未传** preferredId ⇒ 新装分支在 `add()` 内用 `slugify(parsed.name)` 重算 id，已解析的 id 被丢弃。重命名为纯中文 → slugify 返空 → `add` 返 null 技能静默丢失；重命名为新英文名 → 目录分裂 + session_skill_overrides 失效。
- `installBundledSkill` else-分支 `add(`（`:1522`）同样未传 ⇒ SKILL.md `name` 与 assets 目录名不一致时（如 name 含中文），技能落在 `slugify(name)` 目录而附属脚本在 `dirName/` 下。
- `add(` 生产调用点全查：**仅 2 处**（:635 importFromContent、:1522 installBundledSkill），无外部调用方 → 改 `add()` 签名（末位默认参）零破坏。
- `importFromContent` 其余调用方（ConfigBackup:1269 已传 preferredId / SessionForkManager:316 新技能导入应走 slugify / SkillsManagementScreen:465,584,1268 文件与粘贴导入 / SkillRepository:689 URL 导入）不改，默认 null 行为不变。
- **「补一条」复核（resolveImportId 对非法 preferredId 的行为）**：`SkillRestorePolicy.kt:47-48` `if (isAdoptablePreferredId(preferredId)) preferredId!! else fallbackSlug`；`isAdoptablePreferredId`（:36-40）拒绝 null/blank/含 `/`/`\`/`..` ⇒ **非法 preferredId 被拒绝、回落 slugify，不会被静默采信**（既有测试 preferredId_unsafeOrBlank_rejected 8 例已覆盖）。**无需就地修正**；门臂1 钉定 add 首行必须走 resolveImportId，防回归回 `slugify(name)` 直呼。

## ISSUE-10：clearSessionOverrides 零调用 → 跨库残留 —— 成立
- `SkillRepository.kt:398`、`MCPRepository.kt:428` 各有 `fun clearSessionOverrides(sessionId: String)`；`grep -rn clearSessionOverrides src/android/app/src/main` = **只有定义 + KDoc 引用，0 生产调用点**。
- `ChatRepository.deleteSession`（:204）/ `deleteSessionRowOnly`（:251）只删 messages + session 行，无任何跨库清理；无 onSessionDeleted 回调。
- 对照先例：`clearSessionGrants`（OffloadPermissionManager:586）已接线（ChatViewModel onCleared :4178/:4180）。
- 会话删除调用点全查：deleteSession ← ChatMutationMethods:404 / ChatHistoryDrawer:417（deleteSessionAndCleanup）/ SessionForkManager:158（fork 回滚）；deleteSessionRowOnly ← ChatViewModel:4217（cleanupIfEmptyOnExit 空会话自动清扫）。回调挂在 ChatRepository 层则全部路径一次覆盖。
- `MinisApp.kt`：`lateinit var` 字段（:98/:104/:106），构造顺序 :517-524（chatRepository 先于 skill/mcp）⇒ 需把 skill/mcp 构造提前。dbVersion 守卫早退分支（:517 之前 return）不建任何仓储，顺序迁移无影响。

## 必守锚点（既有测试，SkillRestorePolicyTest.kt）
- `:87-97` `importFromContent_resolvesIdThroughPolicy`：切片 `fun importFromContent(`→`fun skillMdPath` 必含 `SkillRestorePolicy.resolveImportId(preferredId, slugify(parsed.name))` 与 `_skills.value.any { it.id == id }` 两行原文 —— 本次改动不触碰这两行，`preferredId = id` 传参在切片内但不破坏 contains 断言。
- `:148-162` `installBundledSkill_upgradeBranch_refreshesFrontmatterNotSource`：切片首处 `if (existing != null) {`→`Upgraded bundled skill` 不含 `importSource =` —— 本次给 else-分支 `add(` 传 `preferredId = dirName`，该分支在切片**之后**，不破坏。
- `grep "fun add(\|slugify(name)\|resolveImportId" src/test` 全部命中在 SkillRestorePolicyTest.kt，无其他锚定。
- ChatRepository 全部构造点（main/androidTest/test）均为位置参数+默认值 ⇒ 新末位构造参（默认 null）零破坏。

## 结论
两条缺陷本体在真树全部复核成立，报告行号可对；报告「报告失真修正」中臆造的对照不涉及本支。修法按任务书 §1/§2 执行。
