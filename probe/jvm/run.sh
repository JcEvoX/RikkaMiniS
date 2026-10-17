#!/bin/sh
# FIX-E JVM harness runner — compile real production logic + JUnit, explicit exits.
set -u
cd "$(dirname "$0")"
KOTLINC=/opt/kotlinc/bin/kotlinc
JVM_LIBS=/opt/jvm-libs
CP="/opt/kotlinc/lib/kotlin-stdlib.jar"
CP="$CP:/root/.gradle/caches/modules-2/files-2.1/com.squareup.okhttp3/okhttp/4.12.0/2f4525d4a200e97e1b87449c2cd9bd2e25b7e8cd/okhttp-4.12.0.jar"
CP="$CP:/root/.gradle/caches/modules-2/files-2.1/com.squareup.okio/okio-jvm/3.9.0/eaa4f7858a1e80908b1a2e861e662edd7c6cbbb5/okio-jvm-3.9.0.jar"
CP="$CP:/root/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlinx/kotlinx-coroutines-core-jvm/1.9.0/9beade4c1c1569e4f36cbd2c37e02e3e41502601/kotlinx-coroutines-core-jvm-1.9.0.jar"
CP="$CP:$JVM_LIBS/junit-4.13.2.jar:$JVM_LIBS/hamcrest-core-1.3.jar:$JVM_LIBS/json-20240303.jar"
A=../../src/android/app/src/main/java/com/rikkaminis/app

rm -rf out junit.log
# 1. extract (fail-closed)
python3 extract.py > extract.log 2>&1
EXTRACT_EXIT=$?
cat extract.log
if [ $EXTRACT_EXIT -ne 0 ]; then echo "HARNESS_EXTRACT_FAIL exit=$EXTRACT_EXIT"; exit 1; fi
grep -q "EXTRACT_OK" extract.log || { echo "HARNESS_EXTRACT_FAIL (no EXTRACT_OK)"; exit 1; }

# 2. compile
nice $KOTLINC $A/provider/CallCancellation.kt Harness.kt VoiceCancelGuardTest.kt \
    -cp "$CP" -d out > compile.log 2>&1
COMPILE_EXIT=$?
if [ $COMPILE_EXIT -ne 0 ]; then
    echo "HARNESS_COMPILE_FAIL exit=$COMPILE_EXIT"
    head -30 compile.log
    exit 1
fi
echo "COMPILE_OK"

# 3. run JUnit (explicit exit code)
java -cp "out:$CP" org.junit.runner.JUnitCore probe.VoiceCancelGuardTest > junit.log 2>&1
JUNIT_EXIT=$?
grep -E "OK \([0-9]+ tests\)|FAILURES|Tests run" junit.log | head -5
if [ $JUNIT_EXIT -ne 0 ]; then
    echo "HARNESS_JUNIT_FAIL exit=$JUNIT_EXIT"
    grep -A5 "^There w" junit.log | head -40
    exit 1
fi
grep -q "OK (" junit.log || { echo "HARNESS_JUNIT_FAIL (no OK line)"; exit 1; }
echo "HARNESS_ALL_OK"
