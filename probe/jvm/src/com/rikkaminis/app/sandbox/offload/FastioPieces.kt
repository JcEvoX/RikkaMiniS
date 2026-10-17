package com.rikkaminis.app.sandbox.offload

internal object FastioPieces {
/**
         * Single-dash long options an agent types out of `find`/`grep` habit,
         * rewritten to the `--` spelling OffloadArgs reads as `key value`.
         * Exact matches only, so a path that merely contains a dash is safe.
         */
        private val SHORT_LONG_OPTIONS = setOf(
            "-name", "-type", "-limit", "-max-entries", "-ignore-case",
        )

internal fun normalizeLongOptionSpelling(argv: List<String>): List<String> =
            argv.map { if (it in SHORT_LONG_OPTIONS) "-$it" else it }

/**
         * Expand `tar`'s clustered short options before the shared parse
         * (`-czf a.tar x` → `--create --gzip --file ... `), because OffloadArgs
         * reads any `-word` as ONE flag. Only `tar` gets this: `-rf` must keep
         * its meaning for `rm`/`cp`.
         *
         * `-f` is dropped rather than mapped to `--file`: its value (the
         * archive) must stay a POSITIONAL, and `--key value` would consume it
         * into `values`. `-C` maps to `--directory` (it also takes a value,
         * and the directory is not a positional). An unknown letter inside a
         * cluster leaves the whole token untouched, so the caller reports it
         * as an unknown option instead of silently dropping it.
         */
        internal fun normalizeTarShorts(argv: List<String>): List<String> {
            // [da1020-C] The guard calls the SAME helper as `isTarCommand` —
            // `firstOrNull() != "tar"` broke whenever a global output flag
            // (`--compact` / `-q` / `--quiet`) preceded the subcommand.
            if (firstSubcommandToken(argv) != "tar") return argv
            val out = ArrayList<String>(argv.size)
            for (token in argv) {
                if (token.length < 2 || token[0] != '-' || token.startsWith("--")) {
                    out.add(token)
                    continue
                }
                val expanded = ArrayList<String>(token.length)
                var known = true
                for (ch in token.substring(1)) {
                    when (ch) {
                        'c' -> expanded.add("--create")
                        'x' -> expanded.add("--extract")
                        'z' -> expanded.add("--gzip")
                        'v' -> expanded.add("--verbose")
                        'C' -> expanded.add("--directory")
                        'f' -> Unit   // the archive value stays a positional
                        else -> known = false
                    }
                }
                if (known) out.addAll(expanded) else out.add(token)
            }
            return out
        }

/**
         * [da1020-C] Boolean flags the shared parser must declare, keyed by
         * whether the invocation is a `tar` command. Kept pure so both arms
         * stay testable off-device:
         *
         * `--recursive` / `--force` / `--ignore-case` are booleans, not
         * `--key value` pairs. Without this declaration OffloadArgs consumes
         * the NEXT token as the option's value, so `rm --recursive /tmp/x`
         * lost the path and reported "missing <path>" instead of deleting
         * anything.
         *
         * The tar verbs (`--create`/`--extract`/`--gzip`/`--verbose`) are
         * modes too, but they are declared ONLY for `tar`. The parser turns
         * any UNKNOWN `--key` into a `--key value` pair, so declaring them
         * for every subcommand made `du --create btree` treat `--create` as
         * a flag and silently walk `btree` (exit 0), where the intended
         * behaviour is "missing <path>" (exit 2). Scope — not acceptance —
         * is the contract.
         */
        internal fun fastioBooleanFlags(isTarCommand: Boolean): Set<String> =
            if (isTarCommand) {
                setOf(
                    "recursive", "force", "ignore-case",
                    // tar verbs: `--create`/`--gzip`/... are modes, never `--key value`.
                    "create", "extract", "gzip", "verbose",
                )
            } else {
                setOf("recursive", "force", "ignore-case")
            }

/**
         * [da1020-C] The subcommand is the first token that is not part of a
         * leading option cluster. Mirrors OffloadArgs' own token rule so the
         * raw-argv scan and the shared parse agree:
         *   - global output flags (`--compact` / `-q` / `--quiet`, see
         *     [OffloadOutput.OUTPUT_FLAGS]) and short flags take no value;
         *   - a `--key value` pair consumes the next token, exactly as an
         *     UNKNOWN `--key` does in OffloadArgs;
         *   - `--key=value` is self-contained;
         *   - the first non-option token names the subcommand.
         * `isTarCommand` and [normalizeTarShorts] both call this, so the flag
         * set and the expander can never drift apart.
         */
        internal fun firstSubcommandToken(rawArgv: List<String>): String? {
            var i = 0
            while (i < rawArgv.size) {
                val token = rawArgv[i]
                if (!token.startsWith("-") || token == "-") return token
                val consumesValue = token.startsWith("--") &&
                    !token.contains('=') &&
                    token.substring(2) !in OffloadOutput.OUTPUT_FLAGS &&
                    rawArgv.getOrNull(i + 1)?.startsWith("-") == false
                i += if (consumesValue) 2 else 1
            }
            return null
        }

}
