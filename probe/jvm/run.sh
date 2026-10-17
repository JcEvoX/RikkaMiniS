#!/bin/sh
# FIX-A device driver — deep-audit-1020 ISSUE-01.
#
# Usage:  sh probe/jvm/run.sh <path-to-ProviderRepository.kt> <label>
#
# Extracts the real `ModelListProviderRegistry.fetchModels(...)` call expression
# from <source>, compiles it against a recording fake registry together with a
# static JUnit test, and prints a verdict.  Exit 0 = GREEN, non-zero = RED.
#
# Run from the repository root.

set -u

SRC="${1:?usage: run.sh <source-file> <label>}"
LABEL="${2:?usage: run.sh <source-file> <label>}"

HERE=$(cd "$(dirname "$0")" && pwd)
WORK="$HERE/work"
LIBS="/opt/jvm-libs"
CORO=$(ls /root/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlinx/kotlinx-coroutines-core-jvm/1.9.0/*/kotlinx-coroutines-core-jvm-1.9.0.jar 2>/dev/null | head -1)
STDLIB="/opt/kotlinc/lib/kotlin-stdlib.jar"

if [ ! -f "$SRC" ]; then echo "VERDICT[$LABEL]=MISSING_SOURCE"; exit 3; fi
if [ -z "$CORO" ]; then echo "VERDICT[$LABEL]=NO_COROUTINES_JAR"; exit 3; fi

CP="$LIBS/junit-4.13.2.jar:$LIBS/hamcrest-core-1.3.jar:$CORO"

rm -rf "$WORK/out" "$WORK/Generated.kt" "$WORK/junit.log" "$WORK/compile.log"
mkdir -p "$WORK/out"

python3 "$HERE/extract.py" --source "$SRC" --out "$WORK" > "$WORK/extract.log" 2>&1
if [ $? -ne 0 ] || ! grep -q "^EXTRACT_OK$" "$WORK/extract.log"; then
    echo "VERDICT[$LABEL]=EXTRACT_FAIL"
    cat "$WORK/extract.log"
    exit 2
fi
grep "^EXTRACTED=" "$WORK/extract.log"

nice /opt/kotlinc/bin/kotlinc -nowarn -cp "$CP" -d "$WORK/out" \
    "$WORK/Generated.kt" "$HERE/src/ExtractedCallForwardsContextTest.kt" \
    > "$WORK/compile.log" 2>&1
if [ $? -ne 0 ]; then
    echo "VERDICT[$LABEL]=COMPILE_FAIL"
    tail -30 "$WORK/compile.log"
    exit 2
fi

java -cp "$WORK/out:$CP:$STDLIB" org.junit.runner.JUnitCore \
    probe.ExtractedCallForwardsContextTest > "$WORK/junit.log" 2>&1
RC=$?
grep -E "PROBE_LAST_CONTEXT|^OK |^Tests run|^FAILURES" "$WORK/junit.log"

if [ $RC -eq 0 ]; then
    echo "VERDICT[$LABEL]=GREEN"
else
    echo "VERDICT[$LABEL]=RED"
fi
exit $RC
