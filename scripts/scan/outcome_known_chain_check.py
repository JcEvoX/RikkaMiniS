#!/usr/bin/env python3
"""deep-audit-1018 ISSUE-03 (P2) — outcomeKnown propagation gate.

Pins the four-layer contract introduced by branch fix/da1018-offload-wiring:

    ExecutionCoordinator.CommandResult.outcomeKnown        (layer 1, verdict)
      -> ToolResultShell.outcomeKnown                      (layer 2, engine)
      -> ToolExecutionResult.outcomeKnown                  (layer 3, VM wrap)
      -> AgentRunEvent.ToolFinished.resultKnown            (layer 4, T7 trace)

Pre-fix defects pinned here:
  L1  ExecutionCoordinator.execute() re-wraps the retry verdict's CommandResult
      for output truncation and DROPPED outcomeKnown, so the SAME command's two
      exits disagreed (executeWithShellRetry stamps it; this wrap did not).
  L2  ToolResultShell had no outcomeKnown field at all.
  L3  ToolExecutionResult had no outcomeKnown field at all.
  L4  ChatPromptAndTools hardcoded `resultKnown = true` for EVERY tool, so a
      withheld shell replay reached the reducer as a known outcome and
      SUCCEED_WITH_UNKNOWN_TOOL could never fire on the shell path.

This gate is the wiring face (the reducer semantics are covered by
AgentRunReducerTest, the mapping by OutcomeKnownPropagationTest); it fails when
any single hop is re-inlined, hardcoded or dropped.

Usage: python3 outcome_known_chain_check.py [ROOT]   (ROOT = repo root)
       python3 outcome_known_chain_check.py --selftest
Exit:  0 = gate green, 1 = violations (or selftest failures).
"""
import os
import re
import sys

COORD = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/ExecutionCoordinator.kt"
SHELL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatShellExecution.kt"
TOOLRES = "src/android/app/src/main/java/com/rikkaminis/app/tools/ToolExecutionResult.kt"
PROMPT = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatPromptAndTools.kt"

CRIT_L1 = "L1 execute() re-wrap carries the retry verdict's outcomeKnown"
CRIT_L2 = "L2 ToolResultShell declares outcomeKnown and stamps it from the coordinator"
CRIT_L3 = "L3 ToolExecutionResult declares outcomeKnown (data-class equality included)"
CRIT_MAP = "L2->L3 the single mapping passes outcomeKnown"
CRIT_L4 = "L4 T7 ToolFinished reads resultKnown from the result object"


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def strip_strings(src):
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)


def find_wrap(text, anchor):
    """Index of the `CommandResult(` call whose tail contains `anchor`."""
    for m in re.finditer(r"CommandResult\s*\(", text):
        seg = text[m.start():m.start() + 700]
        end = seg.find(")\n")
        seg = seg[: end if end > 0 else len(seg)]
        if anchor in seg:
            return seg
    return None


def analyse(text):
    """text = comment-stripped sources. Returns [(criterion, ok, detail, line)]."""
    out = []

    coord = strip_strings(text[COORD])
    wrap = find_wrap(coord, "truncated = outputTruncated")
    line = coord[: coord.find("truncated = outputTruncated")].count("\n") + 1 if wrap else -1
    if wrap is None:
        out.append((CRIT_L1, False, "no execute() re-wrap with truncated = outputTruncated found", -1))
    else:
        ok = "outcomeKnown = result.outcomeKnown" in wrap
        out.append((CRIT_L1, ok, "" if ok else "outcomeKnown dropped in the re-wrap: %r" % wrap[-160:], line))

    shell = strip_strings(text[SHELL])
    decl = re.search(r"internal data class ToolResultShell\s*\(([^)]*)\)", shell, flags=re.S)
    has_decl = bool(decl) and "val outcomeKnown: Boolean" in decl.group(1)
    stamps = "outcomeKnown = result.outcomeKnown" in shell
    out.append(
        (
            CRIT_L2,
            has_decl and stamps,
            "" if (has_decl and stamps) else "declared=%s stamped_from_coordinator=%s" % (has_decl, stamps),
            shell.count("\n") if not (has_decl and stamps) else -1,
        )
    )

    toolres = strip_strings(text[TOOLRES])
    has_field = bool(re.search(r"val outcomeKnown: Boolean", toolres))
    in_equals = "outcomeKnown == other.outcomeKnown" in toolres
    in_hash = "outcomeKnown.hashCode()" in toolres
    out.append(
        (
            CRIT_L3,
            has_field and in_equals and in_hash,
            "" if (has_field and in_equals and in_hash) else "field=%s equals=%s hashCode=%s" % (has_field, in_equals, in_hash),
            -1,
        )
    )

    mapped = "outcomeKnown = outcomeKnown," in shell
    out.append((CRIT_MAP, mapped, "" if mapped else "toToolExecutionResult() does not pass outcomeKnown", -1))

    prompt = strip_strings(text[PROMPT])
    good = re.search(r"AgentRunEvent\.ToolFinished\([^)]*resultKnown = result\.outcomeKnown", prompt) is not None
    bad = re.search(r'AgentRunEvent\.ToolFinished\([^)]*resultKnown = true', prompt) is not None
    out.append(
        (
            CRIT_L4,
            good and not bad,
            "" if (good and not bad) else "wired_to_result=%s hardcoded_true=%s" % (good, bad),
            -1,
        )
    )
    return out


COMPLIANT = {
    COORD: '''
        fun execute(cmd: String): CommandResult {
            val (result, shell) = executeWithShellRetry(cmd)
            val outputTruncated = false
            // [deep-audit-1018 ISSUE-03] carry the verdict's outcomeKnown
            CommandResult(
                output = output,
                exitCode = result.exitCode,
                durationMs = durationMs,
                truncated = outputTruncated,
                outcomeKnown = result.outcomeKnown,
            )
        }
        fun executeWithShellRetry(cmd: String): Pair<CommandResult, Shell> {
            return CommandResult(output, code, ms, truncated, outcomeKnown = verdict.outcomeKnown) to shell
        }
    ''',
    SHELL: '''
        fun run(cmd: String, toolTitle: String, timedOut: Boolean): ToolResultShell {
            val result = coordinator.execute(cmd)
            val withReminder = result.output
            return ToolResultShell(
                output = withReminder,
                success = result.exitCode == 0,
                toolTitle = toolTitle,
                timedOut = timedOut,
                // [deep-audit-1018 ISSUE-03] stamp from the coordinator verdict.
                outcomeKnown = result.outcomeKnown,
            )
        }
        internal fun ToolResultShell.toToolExecutionResult(): ToolExecutionResult =
            ToolExecutionResult(
                output = output,
                success = success,
                toolTitle = toolTitle,
                timedOut = timedOut,
                outcomeKnown = outcomeKnown,
            )
        internal data class ToolResultShell(
            val output: String,
            val success: Boolean,
            val toolTitle: String,
            val timedOut: Boolean,
            val outcomeKnown: Boolean = true,
        )
    ''',
    TOOLRES: '''
        data class ToolExecutionResult(
            val output: String,
            val success: Boolean,
            val outcomeKnown: Boolean = true,
        ) {
            override fun equals(other: Any?): Boolean {
                if (other !is ToolExecutionResult) return false
                return output == other.output && outcomeKnown == other.outcomeKnown
            }
            override fun hashCode(): Int {
                var result = output.hashCode()
                result = 31 * result + outcomeKnown.hashCode()
                return result
            }
        }
    ''',
    PROMPT: '''
        traceObserver.t7Reduce(AgentRunEvent.ToolFinished(name, resultKnown = result.outcomeKnown))
    ''',
}

BREAKERS = {}
# B1: the pre-fix shape — re-wrap drops outcomeKnown
BREAKERS[1] = (
    [(COORD, "                outcomeKnown = result.outcomeKnown,\n", "")],
    CRIT_L1,
)
# B2: ToolResultShell loses the field entirely
BREAKERS[2] = (
    [(SHELL, "            val outcomeKnown: Boolean = true,\n", "")],
    CRIT_L2,
)
# B3: ToolExecutionResult loses the field (and its equality participation)
BREAKERS[3] = (
    [
        (TOOLRES, "            val outcomeKnown: Boolean = true,\n", ""),
        (TOOLRES, " && outcomeKnown == other.outcomeKnown", ""),
        (TOOLRES, "        result = 31 * result + outcomeKnown.hashCode()\n", ""),
    ],
    CRIT_L3,
)
# B4: the mapping re-inlined without the field
BREAKERS[4] = (
    [(SHELL, "                outcomeKnown = outcomeKnown,\n", "")],
    CRIT_MAP,
)
# B5: the pre-fix literal
BREAKERS[5] = (
    [(PROMPT, "resultKnown = result.outcomeKnown", "resultKnown = true")],
    CRIT_L4,
)


def selftest():
    print("-- selftest: compliant fixture green, each breaker red on its own criterion --")
    failures = []
    base = analyse(COMPLIANT)
    reds0 = [c for c, ok, _, _ in base if not ok]
    if reds0:
        failures.append("compliant fixture is red on %s" % reds0)
        print("  [FAIL] compliant fixture red on: %s" % reds0)
    else:
        print("  [OK] compliant fixture green on all %d criteria" % len(base))

    for idx in sorted(BREAKERS):
        pairs, target = BREAKERS[idx]
        mutated = dict(COMPLIANT)
        missing = [name for name, old, _ in pairs if old not in mutated[name]]
        if missing:
            failures.append("breaker %d anchor missing in %s" % (idx, missing))
            print("  [FAIL] breaker %d: anchor not found in fixture" % idx)
            continue
        for name, old, new in pairs:
            mutated[name] = mutated[name].replace(old, new, 1)
        res = analyse(mutated)
        red = [c for c, ok, _, _ in res if not ok]
        if target not in red:
            failures.append("breaker %d did not turn %r red (red=%s)" % (idx, target, red))
            print("  [FAIL] breaker %d -> %r stayed green (red=%s)" % (idx, target, red))
        else:
            side = "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)
            print("  [OK] breaker %d -> red on %r%s" % (idx, target, side))
    return failures


def main():
    if "--selftest" in sys.argv:
        fails = selftest()
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("OUTCOME_KNOWN_CHAIN_SELFTEST=PASS (%d fixtures)" % (1 + len(BREAKERS)))
        return 0

    args = [a for a in sys.argv[1:]]
    if "--root" in args:
        root = args[args.index("--root") + 1]
    else:
        root = args[0] if args else "."
    root = root or "."

    text = {}
    for rel in (COORD, SHELL, TOOLRES, PROMPT):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print("FAIL %s not found under root %s" % (rel, root))
            return 1
        text[rel] = strip_comments(open(path, encoding="utf-8").read())

    results = analyse(text)
    failed = [(c, d) for c, ok, d, _ in results if not ok]
    for c, ok, d, line in results:
        if ok:
            print("  [OK] %s" % c)
        else:
            print("  [FAIL] %s%s - %s" % (c, " (line %d)" % line if line > 0 else "", d))
    print("Summary: %d passed, %d failed" % (len(results) - len(failed), len(failed)))
    if failed:
        print("outcome-known chain gate: RED - the propagation contract is violated")
        return 1
    print("GATE=GREEN outcome_known_chain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
