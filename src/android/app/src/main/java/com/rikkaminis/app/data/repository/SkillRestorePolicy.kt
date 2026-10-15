package com.rikkaminis.app.data.repository

/**
 * Pure decision logic for backup-restore skill imports (BUG-02 family,
 * audit 2026-10-15).
 *
 * Why this exists: [SkillRepository.importFromContent] matched a preferred id
 * only when the skill already existed locally (`_skills.value.any`). On a
 * clean install the list is still empty at restore time, so the exported id
 * was silently dropped and the skill landed under a fresh
 * `slugify(parsed.name)` — breaking `session_skill_overrides` rows and
 * `/var/minis/skills/<id>/` references for any skill whose display name had
 * been changed after the backup was taken.
 *
 * A backup restore is a trusted, user-initiated flow (unlike registry
 * discovery): the id comes from the user's own export, so any non-blank,
 * directory-name-safe id is adopted verbatim. Existence is still checked
 * downstream in `importFromContent`'s replace/add branch — it only decides
 * whether we refresh in place or add, never whether the id survives.
 *
 * Also carries the usage-metadata restore decision (BUG-02B): export wrote
 * installedAt/updatedAt/useCount but import never read them back, so a
 * restore zeroed useCount and re-stamped both timestamps.
 *
 * Kept dependency-free on purpose: SkillRepository needs Android's
 * Context/SQLite and can't be unit-tested on the JVM — the same split
 * [SkillMetadataSync] and [SkillAssetSync] already use.
 */
internal object SkillRestorePolicy {

    /**
     * Whether a preferred id from a backup payload is safe to adopt verbatim:
     * non-blank and a legal directory name (no `/`, no `\`, no `..`) — it
     * becomes `/var/minis/skills/<id>/` on disk.
     */
    fun isAdoptablePreferredId(preferredId: String?): Boolean =
        !preferredId.isNullOrBlank() &&
            !preferredId.contains('/') &&
            !preferredId.contains('\\') &&
            !preferredId.contains("..")

    /**
     * The id an imported skill should land under. A valid preferredId wins
     * over the name slug in all cases: exists locally → replace in place;
     * clean install → adopted as-is instead of silently dropped.
     */
    fun resolveImportId(preferredId: String?, fallbackSlug: String): String =
        if (isAdoptablePreferredId(preferredId)) preferredId!! else fallbackSlug

    /** Usage metadata a restored skill row should carry. */
    data class Usage(val installedAt: Long, val updatedAt: Long, val useCount: Double)

    /**
     * Usage metadata after a restore: only fields the backup actually carries
     * (> 0) are applied — older backups without these keys decode as 0 and
     * leave the freshly-created values untouched.
     */
    fun restoreUsage(current: Usage, backup: Usage): Usage = Usage(
        installedAt = if (backup.installedAt > 0) backup.installedAt else current.installedAt,
        updatedAt = if (backup.updatedAt > 0) backup.updatedAt else current.updatedAt,
        useCount = if (backup.useCount > 0) backup.useCount else current.useCount,
    )
}
