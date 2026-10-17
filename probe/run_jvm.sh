#!/bin/sh
# FIX-A JVM 装置:编译 SkillImportPreferredIdTest + SkillRestorePolicy(纯文件)
# 单次编译传入(铁律 §1),运行时 kotlin-stdlib 必须进 classpath(§2)。
# cwd 必须是 repo-a/src/android,readRepoFile 才能找到 app/src/main/...
set -e
REPO=${1:-/var/minis/shared/work/deep-audit-1021/repo-a}
SRC="$REPO/src/android"
OUT=/tmp/fixa-jvm-out.jar
[ -d "$OUT" ] && rm -f "$OUT"
cd "$SRC"
/opt/kotlinc/bin/kotlinc -cp "/opt/jvm-libs/junit-4.13.2.jar:/opt/jvm-libs/hamcrest-core-1.3.jar" \
  "$REPO/src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRestorePolicy.kt" \
  "$REPO/src/android/app/src/test/java/com/rikkaminis/app/data/repository/SkillImportPreferredIdTest.kt" \
  -d "$OUT" 2>&1 | grep -v "^warning:" | head -20
java -cp "$OUT:/opt/kotlinc/lib/kotlin-stdlib.jar:/opt/jvm-libs/junit-4.13.2.jar:/opt/jvm-libs/hamcrest-core-1.3.jar" \
  org.junit.runner.JUnitCore com.rikkaminis.app.data.repository.SkillImportPreferredIdTest 2>&1 | tail -8
