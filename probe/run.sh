#!/bin/sh
# [da1020-C] Compile + run the JVM harness. Exits non-zero on any failure.
# Serial: only one kotlinc/java at a time (COMMON load discipline).
set -e
cd "$(dirname "$0")/.."   # repo root
rm -f probe/jvm/junit.log probe/jvm/out.log
mkdir -p probe/jvm/out
CP="/opt/jvm-libs/junit-4.13.2.jar:/opt/jvm-libs/hamcrest-core-1.3.jar:/opt/jvm-libs/json-20240303.jar:/opt/kotlinc/lib/kotlin-stdlib.jar"
echo "== extract =="
python3 probe/extract.py
echo "== compile =="
nice /opt/kotlinc/bin/kotlinc -cp "$CP" probe/jvm/src/com/rikkaminis/app/sandbox/offload/OffloadArgs.kt \
  probe/jvm/src/com/rikkaminis/app/sandbox/offload/OffloadOutput.kt \
  probe/jvm/src/com/rikkaminis/app/sandbox/offload/ConfigJoin.kt \
  probe/jvm/src/com/rikkaminis/app/sandbox/offload/BrowserJoin.kt \
  probe/jvm/src/com/rikkaminis/app/sandbox/offload/FastioPieces.kt \
  probe/jvm/src/com/rikkaminis/app/sandbox/offload/OffloadArgsFixCTest.kt \
  -include-runtime -d probe/jvm/out 2> probe/jvm/compile.log || { cat probe/jvm/compile.log; echo "COMPILE-FAIL"; exit 1; }
echo "compile-exit=0"
echo "== run =="
nice java -Drkm.repo="$(pwd)" \
  -cp "probe/jvm/out:$CP" \
  org.junit.runner.JUnitCore com.rikkaminis.app.sandbox.offload.OffloadArgsFixCTest \
  > probe/jvm/junit.log 2>&1
rc=$?
grep -c "^OK\|Tests run" probe/jvm/junit.log || true
tail -5 probe/jvm/junit.log
echo "junit-exit=$rc"
exit $rc
