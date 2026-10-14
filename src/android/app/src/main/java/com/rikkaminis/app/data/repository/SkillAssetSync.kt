package com.rikkaminis.app.data.repository

import java.security.MessageDigest

/**
 * Pure decision logic for the bundled-skill sibling extraction gate in
 * [SkillRepository.installBundledSkill].
 *
 * Why this exists (found 2026-10-14, device-verified): the whole bundled
 * install was gated on the SKILL.md `version` alone. When a bundled *script*
 * changed but the version string stayed put, the gate returned early and the
 * sibling extraction never ran — so the fix shipped inside the APK and reached
 * nobody. Caught in the wild: `FIX-RV-C2`'s zero-count guard was present in
 * `assets/skills/semantic-memory/semantic_memory.py` (22419 B) while the
 * device's live `/var/minis/skills/semantic-memory/semantic_memory.py`
 * (21204 B, both SKILL.md at `version: 1.4.1`) did not contain it. Any
 * "script-only fix" was therefore a dead letter for every existing install —
 * the delivery gap was systemic, not specific to that one guard.
 *
 * The sibling payload is now gated by a content hash: [MANIFEST_NAME] records
 * the hash of what was last extracted, so "the APK's payload changed" is the
 * trigger — not a human remembering to bump a version string.
 *
 * What the version gate still governs: the registry row and the on-disk
 * SKILL.md. A frontmatter-only edit (description / body / version) that should
 * reach the model and the DB still needs a version bump — that path is
 * untouched on purpose, because it is also what protects a user-imported,
 * newer copy from being silently downgraded.
 *
 * Hash contract:
 *  - over the sibling payload only — the top-level SKILL.md is excluded
 *    because the version-gated path writes it; including it would rewrite
 *    unchanged scripts on a frontmatter-only edit;
 *  - entries sorted by relative path, so the hash is independent of
 *    `AssetManager.list` ordering;
 *  - each entry contributes `pathLen|path|byteLen|bytes`, length-delimited so
 *    that a rename (`a`,`bc`) can never collide with a content shuffle
 *    (`ab`,`c`).
 *
 * Kept dependency-free on purpose — `SkillRepository` needs Android's
 * Context/AssetManager and can't run on the JVM, so the hash and manifest
 * contracts live here, the same split [SkillMetadataSync] uses.
 */
// ponytail: 每次启动全量读一遍内置技能资产（4 技能 ≈100KB 兄弟文件）做哈希 | 天花板: 资产涨到 MB 级后启动可感知 | 升级触发: 内置技能资产总体积增长到启动耗时可见，或需要"部分文件更新"时改为构建期生成清单
internal object SkillAssetSync {

    /** Sidecar manifest at the skills root: `dirName=<sha256 hex>` per line. */
    const val MANIFEST_NAME = ".bundled_asset_hashes"

    /**
     * One asset file: `path` is relative to the skill directory (`scripts/x.py`),
     * `bytes` is the full payload.
     */
    data class Entry(val path: String, val bytes: ByteArray) {
        // data class defaults to identity comparison for ByteArray — override
        // so tests (and any future dedup) compare content.
        override fun equals(other: Any?): Boolean =
            this === other || (other is Entry && path == other.path && bytes.contentEquals(other.bytes))

        override fun hashCode(): Int = 31 * path.hashCode() + bytes.contentHashCode()
    }

    /**
     * Stable SHA-256 over the payload. Order-independent (entries are sorted
     * here) and rename-sensitive (paths are part of the digest).
     */
    fun payloadHash(entries: List<Entry>): String {
        val digest = MessageDigest.getInstance("SHA-256")
        for (e in entries.sortedBy { it.path }) {
            val path = e.path.toByteArray(Charsets.UTF_8)
            digest.update(path.size.toString().toByteArray(Charsets.UTF_8))
            digest.update('|'.code.toByte())
            digest.update(path)
            digest.update('|'.code.toByte())
            digest.update(e.bytes.size.toString().toByteArray(Charsets.UTF_8))
            digest.update('|'.code.toByte())
            digest.update(e.bytes)
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    /**
     * True when the payload on disk is not what we last extracted. A `null`
     * stored hash (first install, or a device upgraded from a build that
     * predates the manifest) compares unequal on its own — same behavior as
     * before this gate existed.
     */
    fun needsExtraction(storedHash: String?, currentHash: String): Boolean =
        storedHash != currentHash

    /**
     * The three SKILL.md frontmatter fields a user edits when they fork a
     * shipped skill.
     */
    data class Declaration(val name: String, val description: String, val version: String)

    /**
     * True when a registry row that carries a foreign source label is really
     * *our* copy of a shipped skill, i.e. should be adopted (relabelled).
     *
     * Why this exists (found 2026-10-15, device-verified): [needsExtraction]'s
     * gate sits behind an `importSource == BUNDLED` guard in
     * [SkillRepository.installBundledSkill], and the maintainer device has all
     * four shipped skills labelled `file` (skills UI shows 「文件导入」, row
     * versions equal the shipped ones, on-disk SKILL.md byte-identical to the
     * APK's). The guard therefore skipped exactly the installs the gate was
     * written for — no manifest, no extraction, no log line — so FIX-RV-C2's
     * script still never landed even on a build that contained it.
     *
     * Discriminator: the declaration is what a user edits when forking. A row
     * whose name/description/version still equal the shipped ones is our copy
     * that merely *looks* foreign because of how it was created or imported; a
     * row with an edited declaration is theirs and is left untouched. Parsed
     * values are compared rather than raw bytes so frontmatter formatting
     * written by an older app build doesn't read as "the user forked it".
     */
    fun isAdoptableBundledRow(
        storedSource: String,
        bundledSource: String,
        stored: Declaration,
        bundled: Declaration,
    ): Boolean = storedSource != bundledSource && stored == bundled

    /**
     * Parse the manifest. Tolerates blank lines, stray whitespace and malformed
     * lines (dropped) — a partially written manifest only costs one redundant
     * re-extraction, which is idempotent.
     */
    fun parseManifest(text: String): Map<String, String> {
        val out = mutableMapOf<String, String>()
        for (line in text.lineSequence()) {
            val sep = line.indexOf('=')
            if (sep <= 0) continue
            val name = line.substring(0, sep).trim()
            val hash = line.substring(sep + 1).trim()
            if (name.isEmpty() || hash.isEmpty()) continue
            out[name] = hash
        }
        return out
    }

    /** Render the manifest deterministically (sorted keys, trailing newline). */
    fun renderManifest(hashes: Map<String, String>): String =
        hashes.entries
            .sortedBy { it.key }
            .joinToString(separator = "\n", postfix = "\n") { "${it.key}=${it.value}" }
}
