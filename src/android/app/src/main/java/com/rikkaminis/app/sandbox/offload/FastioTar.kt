package com.rikkaminis.app.sandbox.offload

import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.nio.charset.StandardCharsets
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.FileTime
import java.nio.file.attribute.PosixFilePermission
import java.util.concurrent.TimeUnit

/**
 * Pure tar engine behind `minis-fastio tar` ([T-minis-fastio] phase 3).
 *
 * Top-level and free of Android types (no `Context`, no `org.json`) so both
 * directions are exercised by plain JVM unit tests — and, more importantly, so
 * the round-trip against the GUEST's real `tar` (busybox 1.37 in this rootfs)
 * can be driven from a JVM harness. That interop test is the only honest way to
 * check a container format; a self-consistent reader/writer pair proves
 * nothing about the format.
 *
 * ## Format
 *
 * POSIX ustar: `name` split across `name`/`prefix` when it does not fit the
 * 100-byte field, GNU `L`/`K` long-name entries for the rare path that not even
 * the 155+100 split can hold, and `x`/`g` pax headers consumed (their `path=` /
 * `linkpath=` records honoured) so archives written by GNU tar `--format=pax`
 * or bsdtar remain readable. Everything else is refused or skipped explicitly,
 * never silently.
 *
 * ## Why not the host's `toybox tar`
 *
 * Spawning `/system/bin/toybox` per call means an argv contract, a version
 * contract and an exit-code contract with a binary we do not control, all on
 * the way to the same bytes. This engine is ~330 lines of arithmetic and two
 * header layouts.
 *
 * ## The guard
 *
 * Extraction never writes through a name it has not proven to be inside the
 * destination: absolute names, `..` above the root, and any name whose PARENT
 * chain crosses a symlink are refused (`tar_slip`). The archive is validated in
 * a first pass, so a hostile entry appearing anywhere leaves the destination
 * untouched — refusing halfway would still have written the entries before it.
 */

// ── write ───────────────────────────────────────────────────────────────────

/** Per-entry counters for one `tar -cf`; the handler turns these into JSON. */
internal class TarCreateOutcome : EntryBudget {
    var files = 0L
    var dirs = 0L
    var symlinks = 0L
    var bytes = 0L

    /** Named pipes, sockets, devices: not representable, reported not dropped. */
    var skippedSpecial = 0L
    val skippedNames = ArrayList<String>()
    override var visited = 0L
    override var truncated = false
    val errors = ArrayList<String>()
}

/**
 * Write [roots] (host path + the member-name prefix it is stored under) into
 * [sink] as a POSIX ustar stream. The prefix is the guest path AS THE USER
 * SPELLED IT, put through [tarMemberPrefix]: `tar -cf a.tar src` stores
 * `src/...`, which is what GNU/busybox store and what makes a third-party
 * `tar -xf` land the tree where the caller meant. Naming members after the
 * RESOLVED path instead nests every extraction under `dest/var/minis/...`.
 *
 * Symlinks are written AS LINKS (never followed), matching the rest of the
 * handler. Everything that is neither a regular file, a directory nor a
 * symlink is counted in [TarCreateOutcome.skippedSpecial] with up to
 * [MAX_REPORTED_SKIPS] names, so an archive that quietly lacks a device node
 * says so.
 */
internal fun tarCreate(
    sink: OutputStream,
    roots: List<Pair<Path, String>>,
    maxEntries: Long,
    out: TarCreateOutcome,
) {
    for ((path, memberPrefix) in roots) {
        val name = tarMemberName(memberPrefix)
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            out.errors.add("$memberPrefix: not found")
            continue
        }
        if (Files.isSymbolicLink(path)) {
            writeLink(sink, name, path, out)
            continue
        }
        if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
            writeRegular(sink, name, path, out)
            continue
        }
        try {
            Files.walkFileTree(
                path,
                object : SimpleFileVisitor<Path>() {
                    override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                        if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                        val entry = memberNameFor(name, path, dir)
                        writeHeader(sink, header(entry + "/", '5', 0L, dirMode(dir), mtimeOf(attrs), ""))
                        out.dirs++
                        return FileVisitResult.CONTINUE
                    }

                    override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                        if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                        val entry = memberNameFor(name, path, file)
                        if (attrs.isSymbolicLink) {
                            writeLink(sink, entry, file, out)
                        } else if (attrs.isRegularFile) {
                            writeRegular(sink, entry, file, out)
                        } else {
                            out.skippedSpecial++
                            if (out.skippedNames.size < MAX_REPORTED_SKIPS) out.skippedNames.add(entry)
                        }
                        return FileVisitResult.CONTINUE
                    }

                    override fun visitFileFailed(file: Path, exc: java.io.IOException): FileVisitResult {
                        out.errors.add("$file: ${exc.message}")
                        return FileVisitResult.CONTINUE
                    }
                },
            )
        } catch (e: Exception) {
            out.errors.add("$memberPrefix: ${e.message}")
        }
    }
    // Two zero blocks end the archive; readers stop on the first one.
    sink.write(ByteArray(BLOCK_SIZE * 2))
    sink.flush()
}

private fun writeRegular(sink: OutputStream, name: String, file: Path, out: TarCreateOutcome) {
    val size = try {
        Files.size(file)
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    writeHeader(sink, header(name, '0', size, modeOf(file), mtimeOf(file), ""))
    out.visited++
    try {
        Files.newInputStream(file).use { input -> copy(input, sink, size) }
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    // Every member's payload is padded to a 512-byte boundary — without this
    // the next header starts mid-block and the whole archive after it is
    // garbage to a real reader (and to ours).
    val pad = ((BLOCK_SIZE - (size % BLOCK_SIZE)) % BLOCK_SIZE).toInt()
    if (pad > 0) sink.write(ByteArray(pad))
    out.files++
    out.bytes += size
}

private fun writeLink(sink: OutputStream, name: String, file: Path, out: TarCreateOutcome) {
    val target = try {
        Files.readSymbolicLink(file).toString()
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    writeHeader(sink, header(name, '2', 0L, modeOf(file), mtimeOf(file), target))
    out.visited++
    out.symlinks++
}

private fun memberNameFor(rootName: String, root: Path, path: Path): String {
    val relative = root.relativize(path).toString().replace('\\', '/')
    return if (relative.isEmpty()) rootName else "$rootName/$relative"
}

/** A guest path as a tar member name: `tar` stores absolute arguments without the leading `/`. */
internal fun tarMemberName(guestPath: String): String =
    guestPath.trimStart('/').ifEmpty { "." }

/**
 * The member-name prefix for one `tar -c` argument: the argument **as the user
 * spelled it**.
 *
 * `tar` stores the name it was GIVEN, not the path it resolved — that is what
 * makes a `tar -cf a.tar src` archive extract to `./src` for whoever reads it.
 * Naming members after the resolved guest path instead yields
 * `var/minis/workspace/src/...`, which busybox unpacks into a nested
 * `dest/var/minis/workspace/src` tree and which no `-C` can undo (review
 * finding F1, probe `shared/work/fix-fastio-tarname-1001/jvm`).
 *
 * The normalizations GNU and busybox tar both apply to an argument:
 *   - a leading `/` is dropped (`tar: removing leading '/' from member names`);
 *   - leading `../` sequences are dropped (`tar: removing leading '../' from
 *     member names`). An argument that is nothing BUT `..` therefore keeps no
 *     spelling at all; `.` is used, which extracts to the same place busybox's
 *     stripped name would;
 *   - a trailing `/` is dropped, because a directory entry writes its own.
 *
 * `./` is preserved (`./src/f1.txt`), as both readers do. An inner `..`
 * (`a/../b`) is left as spelled, again matching both tars — note that such a
 * member is refused by OUR extractor as `tar_slip`, so pass a normalized
 * argument if the archive is meant to come back.
 */
internal fun tarMemberPrefix(raw: String): String {
    var rest = raw.trimStart('/')
    while (rest == ".." || rest.startsWith("../")) {
        rest = rest.removePrefix("..").removePrefix("/")
    }
    return rest.trimEnd('/').ifEmpty { "." }
}

/** One header's worth of fields, before serialization. */
internal data class TarMember(
    val name: String,
    val type: Char,
    val size: Long,
    val mode: Int,
    val mtimeSeconds: Long,
    val linkName: String,
)

private fun header(
    name: String,
    type: Char,
    size: Long,
    mode: Int,
    mtimeSeconds: Long,
    linkName: String,
): TarMember = TarMember(name, type, size, mode, mtimeSeconds, linkName)

/* file → header fields */

private fun mtimeOf(attrs: BasicFileAttributes): Long =
    try { attrs.lastModifiedTime().to(TimeUnit.SECONDS) } catch (_: Exception) { 0L }

private fun mtimeOf(path: Path): Long =
    try { Files.getLastModifiedTime(path, LinkOption.NOFOLLOW_LINKS).to(TimeUnit.SECONDS) } catch (_: Exception) { 0L }

private fun modeOf(path: Path): Int =
    try {
        val perms = Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS)
        permissionBits(perms, isDirectory = Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS))
    } catch (_: Exception) {
        0b110_100_100
    }

private fun dirMode(path: Path): Int =
    try {
        val perms = Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS)
        permissionBits(perms, isDirectory = true)
    } catch (_: Exception) {
        0b111_101_101
    }

private fun permissionBits(perms: Set<PosixFilePermission>, isDirectory: Boolean): Int {
    var bits = if (isDirectory) 0b100_000_000_000 else 0
    val table = listOf(
        PosixFilePermission.OWNER_READ to 0b100_000_000,
        PosixFilePermission.OWNER_WRITE to 0b010_000_000,
        PosixFilePermission.OWNER_EXECUTE to 0b001_000_000,
        PosixFilePermission.GROUP_READ to 0b000_100_000,
        PosixFilePermission.GROUP_WRITE to 0b000_010_000,
        PosixFilePermission.GROUP_EXECUTE to 0b000_001_000,
        PosixFilePermission.OTHERS_READ to 0b000_000_100,
        PosixFilePermission.OTHERS_WRITE to 0b000_000_010,
        PosixFilePermission.OTHERS_EXECUTE to 0b000_000_001,
    )
    for ((perm, bit) in table) if (perm in perms) bits = bits or bit
    return bits
}

// ── read ────────────────────────────────────────────────────────────────────

/** Per-entry counters for one `tar -xf`; the handler turns these into JSON. */
internal class TarExtractOutcome : EntryBudget {
    var files = 0L
    var dirs = 0L
    var symlinks = 0L
    var bytes = 0L
    var skippedSpecial = 0L
    var skippedHardlink = 0L
    var paxHeaders = 0L
    var longNameHeaders = 0L
    override var visited = 0L
    override var truncated = false
    val errors = ArrayList<String>()
    val skippedNames = ArrayList<String>()
}

/** Why a member must not be written. Reported verbatim to the agent. */
internal sealed class TarRefusal {
    data class Slip(val name: String, val detail: String) : TarRefusal()
    data class BadHeader(val detail: String) : TarRefusal()
}

/** Where a validated member lands, and how. */
internal data class TarTarget(val path: Path, val member: TarMember)

/**
 * Validate [source] end to end WITHOUT writing anything: every header is
 * parsed, every name is checked against [dest]. This is what makes "refuse and
 * leave nothing behind" true for a hostile entry that appears anywhere in the
 * archive, instead of only for the entries before it.
 *
 * @return true when the archive is safe to extract; false means [out] holds the
 * reason and the caller must not call [tarExtract].
 */
internal fun tarValidate(
    source: InputStream,
    dest: Path,
    maxEntries: Long,
    out: TarExtractOutcome,
): Boolean {
    val reader = TarReader(source)
    val declaredLinks = HashSet<String>()
    try {
        while (true) {
            val member = reader.next() ?: break
            if (charge(out, maxEntries)) {
                out.errors.add("entry budget reached while validating; extraction would be partial")
                return false
            }
            val refusal = tarEntryTarget(dest, member, declaredLinks)
            if (refusal != null) {
                out.errors.add(describe(refusal))
                return false
            }
            if (member.type == '2') {
                tarNormalizeMember(member.name)?.let { declaredLinks.add(it.joinToString("/")) }
            }
            reader.skipData()
        }
    } catch (e: Exception) {
        out.errors.add("archive: ${e.message}")
        return false
    }
    return true
}

/**
 * Extract every member of [source] under [dest]. [force] replaces an existing
 * regular file or symlink; an existing DIRECTORY is always reused (re-extracting
 * over a tree is the normal case and a directory is never "overwritten").
 * Call [tarValidate] on the same archive first — this pass re-checks each name
 * anyway (cheap, same rule) but assumes the caller has already refused a
 * hostile archive.
 */
internal fun tarExtract(
    source: InputStream,
    dest: Path,
    force: Boolean,
    maxEntries: Long,
    out: TarExtractOutcome,
) {
    val reader = TarReader(source)
    val declaredLinks = HashSet<String>()
    try {
        Files.createDirectories(dest)
        while (true) {
            val member = reader.next() ?: break
            if (charge(out, maxEntries)) break

            val refusal = tarEntryTarget(dest, member, declaredLinks)
            if (refusal != null) {
                out.errors.add(describe(refusal))
                break
            }
            val normalized = tarNormalizeMember(member.name)
            val target = dest.resolve(member.name).normalize()

            try {
                when (member.type) {
                    '5' -> {
                        Files.createDirectories(target)
                        out.dirs++
                    }
                    '2' -> {
                        if (Files.exists(target, LinkOption.NOFOLLOW_LINKS)) {
                            if (!force) {
                                out.errors.add("${member.name}: already_exists (pass --force to replace)")
                                reader.skipData()
                                continue
                            }
                            Files.delete(target)
                        }
                        Files.createDirectories(target.parent)
                        Files.createSymbolicLink(target, java.nio.file.Paths.get(member.linkName))
                        normalized?.let { declaredLinks.add(it.joinToString("/")) }
                        out.symlinks++
                    }
                    '1' -> {
                        // Hard links are not materialized in v1: the file is
                        // already in the archive as a regular member, so the
                        // content is not lost — the sharing is. Counted, not silent.
                        out.skippedHardlink++
                        if (out.skippedNames.size < MAX_REPORTED_SKIPS) out.skippedNames.add(member.name)
                    }
                    '0', '\u0000', '7' -> {
                        if (Files.exists(target, LinkOption.NOFOLLOW_LINKS)) {
                            if (!force) {
                                out.errors.add("${member.name}: already_exists (pass --force to replace)")
                                reader.skipData()
                                continue
                            }
                            if (Files.isDirectory(target, LinkOption.NOFOLLOW_LINKS)) {
                                out.errors.add("${member.name}: target is a directory; refusing to replace it")
                                reader.skipData()
                                continue
                            }
                            Files.delete(target)
                        }
                        Files.createDirectories(target.parent)
                        Files.newOutputStream(target).use { sink -> reader.copyData(sink, member.size) }
                        applyMetadata(target, member)
                        out.files++
                        out.bytes += member.size
                    }
                    else -> {
                        out.skippedSpecial++
                        if (out.skippedNames.size < MAX_REPORTED_SKIPS) out.skippedNames.add(member.name)
                    }
                }
            } catch (e: Exception) {
                out.errors.add("${member.name}: ${e.message}")
            }
            reader.skipData()
        }
    } catch (e: Exception) {
        out.errors.add("archive: ${e.message}")
    } finally {
        out.paxHeaders = reader.paxHeaders
    }
}

private fun describe(refusal: TarRefusal): String = when (refusal) {
    is TarRefusal.Slip -> "tar_slip: ${refusal.detail}"
    is TarRefusal.BadHeader -> "bad_header: ${refusal.detail}"
}

/**
 * Split a member name into path segments, or null when the name itself is
 * illegal: absolute, containing NUL, resolving to the root, or climbing above
 * it. Pure — the same rule is applied in the validation pass and again in the
 * extraction pass, so the two can never disagree about what a name means.
 */
internal fun tarNormalizeMember(name: String): List<String>? {
    if (name.isEmpty()) return null
    if (name.startsWith("/")) return null
    if ('\u0000' in name) return null
    val segments = ArrayList<String>()
    for (segment in name.split('/')) {
        when (segment) {
            "", "." -> Unit
            ".." -> {
                if (segments.isEmpty()) return null
                segments.removeAt(segments.size - 1)
            }
            else -> segments.add(segment)
        }
    }
    return segments.ifEmpty { null }
}

/**
 * The tar-slip rule, applied to ONE member. Returns null when [member] may be
 * written under [dest].
 *
 * Four ways out of the destination are refused:
 *   1. an absolute name (`/etc/passwd`);
 *   2. a name whose `..` segments climb above the destination root;
 *   3. a name whose PARENT chain crosses a symlink that already exists on disk
 *      — the classic second-order attack, where an earlier extraction (or the
 *      user) left `link -> /` behind and the archive writes through `link/etc`;
 *   4. the same chain crossing a symlink THIS archive declares ([declaredLinks],
 *      normalized names of type `2` members already seen). Rule 3 cannot see
 *      that one: the link is created during extraction, after validation has
 *      to have decided. Checking it in the validation pass is what keeps
 *      "a hostile archive leaves nothing behind" true for the whole archive.
 *
 * The last segment is deliberately NOT followed: writing onto an existing
 * symlink is what `force` is for, and it replaces the link rather than its
 * target (`Files.delete` below).
 */
internal fun tarEntryTarget(
    dest: Path,
    member: TarMember,
    declaredLinks: Set<String> = emptySet(),
): TarRefusal? {
    val segments = tarNormalizeMember(member.name)
        ?: return TarRefusal.Slip(member.name, "entry '${member.name}' is absolute, contains NUL, or escapes the extraction root")

    var current = dest
    for (i in 0 until segments.size - 1) {
        current = current.resolve(segments[i])
        val prefix = segments.subList(0, i + 1).joinToString("/")
        if (prefix in declaredLinks) {
            return TarRefusal.Slip(
                member.name,
                "entry '${member.name}' writes through the symlink '$prefix' declared by this archive — refusing (tar slip)",
            )
        }
        if (Files.isSymbolicLink(current)) {
            return TarRefusal.Slip(
                member.name,
                "entry '${member.name}' writes through the symlink '${current}' — refusing (tar slip)",
            )
        }
    }
    return null
}

private fun applyMetadata(target: Path, member: TarMember) {
    try {
        Files.setLastModifiedTime(target, FileTime.from(member.mtimeSeconds, TimeUnit.SECONDS))
    } catch (_: Exception) {
        // Not fatal: an mtime we could not set must not fail the extraction.
    }
    if (member.mode == 0) return
    try {
        Files.setPosixFilePermissions(target, permissionsOf(member.mode))
    } catch (_: Exception) {
        // Non-POSIX filesystems (or a mode the platform rejects) — content is
        // what matters; the mode is best-effort exactly like `tar` without -p.
    }
}

internal fun permissionsOf(mode: Int): Set<PosixFilePermission> {
    val out = HashSet<PosixFilePermission>()
    val table = listOf(
        PosixFilePermission.OWNER_READ to 0b100_000_000,
        PosixFilePermission.OWNER_WRITE to 0b010_000_000,
        PosixFilePermission.OWNER_EXECUTE to 0b001_000_000,
        PosixFilePermission.GROUP_READ to 0b000_100_000,
        PosixFilePermission.GROUP_WRITE to 0b000_010_000,
        PosixFilePermission.GROUP_EXECUTE to 0b000_001_000,
        PosixFilePermission.OTHERS_READ to 0b000_000_100,
        PosixFilePermission.OTHERS_WRITE to 0b000_000_010,
        PosixFilePermission.OTHERS_EXECUTE to 0b000_000_001,
    )
    for ((perm, bit) in table) if (mode and bit != 0) out.add(perm)
    return out
}

/**
 * Streaming ustar reader: `next()` returns the next logical member (resolving
 * GNU `L`/`K` long names and pax `path=`/`linkpath=` records into it), and the
 * caller then either [copyData] or [skipData].
 *
 * The contract is "always consume the data before calling `next()` again" —
 * enforced by tracking how many bytes of the current member are outstanding,
 * so a caller that forgot to consume them cannot silently desynchronize.
 */
internal class TarReader(private val source: InputStream) {
    private var pendingData = 0L
    private var longName: String? = null
    private var longLink: String? = null
    private var paxPath: String? = null
    private var paxLink: String? = null

    /** pax extension headers consumed so far (only path/linkpath are honoured). */
    var paxHeaders = 0L
        private set

    fun next(): TarMember? {
        requirePaused()
        var zeroBlocks = 0
        while (true) {
            val block = readBlock() ?: return null
            if (block.all { it == 0.toByte() }) {
                zeroBlocks++
                if (zeroBlocks >= 2) return null
                continue
            }
            val member = parseHeader(block)
            when (member.type) {
                'L' -> {
                    longName = readText(member.size)
                    continue
                }
                'K' -> {
                    longLink = readText(member.size)
                    continue
                }
                'x', 'g' -> {
                    paxHeaders++
                    applyPax(readText(member.size))
                    continue
                }
                else -> {
                    val name = paxPath ?: longName ?: member.name
                    val link = paxLink ?: longLink ?: member.linkName
                    paxPath = null
                    paxLink = null
                    longName = null
                    longLink = null
                    pendingData = member.size
                    return member.copy(name = name, linkName = link)
                }
            }
        }
    }

    /** Copy the current member's payload, rejecting a truncated archive. */
    fun copyData(sink: OutputStream, size: Long) {
        if (size.toLong() != pendingData) {
            // A pax `size=` override we do not model: copy what the header says.
            pendingData = size
        }
        val buffer = ByteArray(COPY_BUFFER)
        var left = pendingData
        while (left > 0) {
            val read = source.read(buffer, 0, minOf(buffer.size.toLong(), left).toInt())
            if (read < 0) throw EOFException("archive truncated ${left} bytes into an entry")
            sink.write(buffer, 0, read)
            left -= read
        }
        pendingData = 0
        skipPadding(size)
    }

    /** Consume the current member's payload without copying it. */
    fun skipData() {
        skipBytes(pendingData)
        skipPadding(pendingData)
        pendingData = 0
    }

    private fun requirePaused() {
        if (pendingData != 0L) {
            throw IllegalStateException("$pendingData bytes of the previous member were not consumed")
        }
    }

    private fun readBlock(): ByteArray? {
        val block = ByteArray(BLOCK_SIZE)
        var filled = 0
        while (filled < BLOCK_SIZE) {
            val read = source.read(block, filled, BLOCK_SIZE - filled)
            if (read < 0) return if (filled == 0) null else throw EOFException("partial block at end of archive")
            filled += read
        }
        return block
    }

    private fun readText(size: Long): String {
        if (size <= 0 || size > MAX_LONG_NAME_BYTES) throw EOFException("implausible long-name size $size")
        val bytes = ByteArray(size.toInt())
        var filled = 0
        while (filled < bytes.size) {
            val read = source.read(bytes, filled, bytes.size - filled)
            if (read < 0) throw EOFException("archive truncated in a long-name record")
            filled += read
        }
        skipPadding(size)
        return bytes.toString(StandardCharsets.UTF_8).trimEnd('\u0000')
    }

    private fun skipBytes(count: Long) {
        var left = count
        val buffer = ByteArray(COPY_BUFFER)
        while (left > 0) {
            val read = source.read(buffer, 0, minOf(buffer.size.toLong(), left).toInt())
            if (read < 0) throw EOFException("archive truncated ${left} bytes into an entry")
            left -= read
        }
    }

    private fun skipPadding(size: Long) {
        val pad = (BLOCK_SIZE - (size % BLOCK_SIZE)) % BLOCK_SIZE
        if (pad > 0) skipBytes(pad)
    }

    private fun applyPax(text: String) {
        // Records are "LENGTH KEY=VALUE\n"; we honour only the two path
        // overrides and ignore the rest (mtime/size/uid are advisory here).
        var cursor = 0
        while (cursor < text.length) {
            val space = text.indexOf(' ', cursor)
            if (space < 0) break
            val length = text.substring(cursor, space).toIntOrNull() ?: break
            if (length <= 0 || cursor + length > text.length) break
            val record = text.substring(space + 1, cursor + length).trimEnd('\n')
            val eq = record.indexOf('=')
            if (eq > 0) {
                when (record.substring(0, eq)) {
                    "path" -> paxPath = record.substring(eq + 1)
                    "linkpath" -> paxLink = record.substring(eq + 1)
                }
            }
            cursor += length
        }
    }
}

/** Parse one 512-byte header block. Throws [IllegalStateException] on a bad checksum. */
internal fun parseHeader(block: ByteArray): TarMember {
    val expected = parseOctal(block, CHECKSUM_OFFSET, CHECKSUM_LENGTH, signed = true)
    val actual = checksum(block)
    if (expected != actual) {
        throw IllegalStateException("header checksum mismatch (stored %s, computed %s)".format(expected, actual))
    }
    val size = parseOctal(block, SIZE_OFFSET, SIZE_LENGTH, signed = false)
    val mode = parseOctal(block, MODE_OFFSET, MODE_LENGTH, signed = false).toInt()
    val mtime = parseOctal(block, MTIME_OFFSET, MTIME_LENGTH, signed = false)
    val rawName = text(block, NAME_OFFSET, NAME_LENGTH)
    val prefix = if (magic(block)) text(block, PREFIX_OFFSET, PREFIX_LENGTH) else ""
    val name = if (prefix.isEmpty()) rawName else "$prefix/$rawName"
    val linkName = text(block, LINKNAME_OFFSET, LINKNAME_LENGTH)
    return TarMember(name, block[TYPEFLAG_OFFSET].toInt().toChar(), size, mode, mtime, linkName)
}

private fun magic(block: ByteArray): Boolean =
    block[MAGIC_OFFSET] == 'u'.code.toByte() && block[MAGIC_OFFSET + 1] == 's'.code.toByte()

private fun checksum(block: ByteArray): Long {
    var sum = 0L
    for (i in block.indices) {
        sum += if (i in CHECKSUM_OFFSET until CHECKSUM_OFFSET + CHECKSUM_LENGTH) 32L else (block[i].toLong() and 0xFF)
    }
    return sum
}

/** Octal field → value. Base-256 (high bit set) is refused rather than misread. */
private fun parseOctal(block: ByteArray, offset: Int, length: Int, signed: Boolean): Long {
    val first = block[offset].toInt() and 0xFF
    if (first and 0x80 != 0) throw IllegalStateException("base-256 numeric fields are not supported")
    var value = 0L
    for (i in offset until offset + length) {
        val c = block[i].toInt() and 0xFF
        if (c == 0 || c == ' '.code) continue
        if (c < '0'.code || c > '7'.code) {
            if (signed) return -1L
            throw IllegalStateException("non-octal byte in a numeric field")
        }
        value = value * 8 + (c - '0'.code)
    }
    return value
}

private fun text(block: ByteArray, offset: Int, length: Int): String =
    block.copyOfRange(offset, offset + length)
        .toString(StandardCharsets.UTF_8)
        .substringBefore('\u0000')

// ── serialization ───────────────────────────────────────────────────────────

private fun writeHeader(sink: OutputStream, member: TarMember) {
    val block = ByteArray(BLOCK_SIZE)
    val split = splitUstarName(member.name)
    if (split == null) {
        // GNU long name: an out-of-band 'L' member carries the full path.
        val bytes = member.name.toByteArray(StandardCharsets.UTF_8) + 0
        putText(block, NAME_OFFSET, NAME_LENGTH, "././@LongLink")
        putInt(block, MODE_OFFSET, MODE_LENGTH, 0b110_100_100)
        putInt(block, SIZE_OFFSET, SIZE_LENGTH, bytes.size.toLong())
        putInt(block, MTIME_OFFSET, MTIME_LENGTH, member.mtimeSeconds)
        block[TYPEFLAG_OFFSET] = 'L'.code.toByte()
        putText(block, MAGIC_OFFSET, MAGIC_LENGTH, "ustar")
        putFixed(block, VERSION_OFFSET, VERSION_LENGTH, "00")
        finishBlock(block)
        sink.write(block)
        sink.write(bytes)
        sink.write(ByteArray(((BLOCK_SIZE - (bytes.size % BLOCK_SIZE)) % BLOCK_SIZE)))
        // The real header repeats the name, truncated to the field.
        putText(block, NAME_OFFSET, NAME_LENGTH, member.name)
        putText(block, PREFIX_OFFSET, PREFIX_LENGTH, "")
    } else {
        putText(block, NAME_OFFSET, NAME_LENGTH, split.second)
        putText(block, PREFIX_OFFSET, PREFIX_LENGTH, split.first)
    }
    putInt(block, MODE_OFFSET, MODE_LENGTH, member.mode.toLong())
    putInt(block, UID_OFFSET, UID_LENGTH, 0L)
    putInt(block, GID_OFFSET, GID_LENGTH, 0L)
    putInt(block, SIZE_OFFSET, SIZE_LENGTH, member.size)
    putInt(block, MTIME_OFFSET, MTIME_LENGTH, member.mtimeSeconds)
    block[TYPEFLAG_OFFSET] = member.type.code.toByte()
    putText(block, LINKNAME_OFFSET, LINKNAME_LENGTH, member.linkName)
    putText(block, MAGIC_OFFSET, MAGIC_LENGTH, "ustar")
    putFixed(block, VERSION_OFFSET, VERSION_LENGTH, "00")
    putText(block, UNAME_OFFSET, UNAME_LENGTH, "")
    putText(block, GNAME_OFFSET, GNAME_LENGTH, "")
    finishBlock(block)
    sink.write(block)
}

/**
 * Split [name] across the `name`/`prefix` pair when it does not fit 100 bytes.
 * The LAST '/' that leaves both halves in range wins, because a larger prefix
 * keeps the (100-byte) name field as short as possible.
 * Returns null when no split fits — the caller then emits a GNU long name.
 */
internal fun splitUstarName(name: String): Pair<String, String>? {
    if (utf8Length(name) <= NAME_LENGTH) return "" to name
    var best = -1
    for (i in name.indices) {
        if (name[i] != '/') continue
        val prefix = name.substring(0, i)
        val rest = name.substring(i + 1)
        if (rest.isEmpty()) continue
        if (utf8Length(prefix) <= PREFIX_LENGTH && utf8Length(rest) <= NAME_LENGTH) best = i
    }
    if (best < 0) return null
    return name.substring(0, best) to name.substring(best + 1)
}

private fun utf8Length(s: String): Int = s.toByteArray(StandardCharsets.UTF_8).size

private fun finishBlock(block: ByteArray) {
    for (i in CHECKSUM_OFFSET until CHECKSUM_OFFSET + CHECKSUM_LENGTH) block[i] = ' '.code.toByte()
    val sum = checksum(block)
    val text = octal(sum, 6)
    for (i in text.indices) block[CHECKSUM_OFFSET + i] = text[i].code.toByte()
    block[CHECKSUM_OFFSET + 6] = 0
    block[CHECKSUM_OFFSET + 7] = ' '.code.toByte()
}

/** Fixed-width field with NO terminator (magic / version): every byte counts. */
private fun putFixed(block: ByteArray, offset: Int, length: Int, value: String) {
    val bytes = value.toByteArray(StandardCharsets.UTF_8)
    System.arraycopy(bytes, 0, block, offset, minOf(bytes.size, length))
}

private fun putText(block: ByteArray, offset: Int, length: Int, value: String) {
    val bytes = value.toByteArray(StandardCharsets.UTF_8)
    val count = minOf(bytes.size, length - 1)
    System.arraycopy(bytes, 0, block, offset, count)
}

private fun putInt(block: ByteArray, offset: Int, length: Int, value: Long) {
    val text = octal(value, length - 1)
    for (i in text.indices) block[offset + i] = text[i].code.toByte()
}

private fun octal(value: Long, digits: Int): String {
    val text = java.lang.Long.toOctalString(if (value < 0) 0L else value)
    return if (text.length >= digits) text.substring(text.length - digits) else "0".repeat(digits - text.length) + text
}

private fun copy(input: InputStream, sink: OutputStream, size: Long) {
    val buffer = ByteArray(COPY_BUFFER)
    var left = size
    while (left > 0) {
        val read = input.read(buffer, 0, minOf(buffer.size.toLong(), left).toInt())
        if (read < 0) throw EOFException("file shrank while archiving (${left} bytes short)")
        sink.write(buffer, 0, read)
        left -= read
    }
}

// ── constants ───────────────────────────────────────────────────────────────

private const val BLOCK_SIZE = 512
private const val NAME_OFFSET = 0
private const val NAME_LENGTH = 100
private const val MODE_OFFSET = 100
private const val MODE_LENGTH = 8
private const val UID_OFFSET = 108
private const val UID_LENGTH = 8
private const val GID_OFFSET = 116
private const val GID_LENGTH = 8
private const val SIZE_OFFSET = 124
private const val SIZE_LENGTH = 12
private const val MTIME_OFFSET = 136
private const val MTIME_LENGTH = 12
private const val CHECKSUM_OFFSET = 148
private const val CHECKSUM_LENGTH = 8
private const val TYPEFLAG_OFFSET = 156
private const val LINKNAME_OFFSET = 157
private const val LINKNAME_LENGTH = 100
private const val MAGIC_OFFSET = 257
private const val MAGIC_LENGTH = 6
private const val VERSION_OFFSET = 263
private const val VERSION_LENGTH = 2
private const val UNAME_OFFSET = 265
private const val UNAME_LENGTH = 32
private const val GNAME_OFFSET = 297
private const val GNAME_LENGTH = 32
private const val PREFIX_OFFSET = 345
private const val PREFIX_LENGTH = 155

private const val COPY_BUFFER = 64 * 1024

/** Names listed in `skipped_names`; the COUNT is always exact. */
private const val MAX_REPORTED_SKIPS = 20

/**
 * ponytail: a pax/GNU long-name record above 1 MiB is refused rather than
 * allocated — the field is a path, and a 1 MiB "path" is not one.
 * 天花板: a real archive with a path longer than 1 MiB (it cannot exist on any
 * filesystem this handler can reach).
 * 升级触发: an archive that legitimately needs it fails with a clear error.
 */
private const val MAX_LONG_NAME_BYTES = 1L * 1024 * 1024
