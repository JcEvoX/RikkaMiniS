#!/bin/sh
# [da1020-C] One-mutation-one-invocation orchestration (COMMON load discipline:
# no long-running driver that outlives the shell; restore always runs).
# Usage: sh probe/run-mutation.sh M1 M2 M3 M4
set -u
cd "$(dirname "$0")/.."   # repo root
: > probe/mutation-results.txt
for mid in "$@"; do
  python3 probe/mutations.py backup "$mid" > probe/mut-$mid-backup.log 2>&1 || { echo "$mid BACKUP-FAIL" >> probe/mutation-results.txt; continue; }
  python3 probe/mutations.py apply "$mid" > probe/mut-$mid-apply.log 2>&1 || { echo "$mid APPLY-FAIL" >> probe/mutation-results.txt; python3 probe/mutations.py restore "$mid" >> probe/mut-$mid-apply.log 2>&1; continue; }
  python3 scripts/scan/offload_cli_args_check.py . > probe/mut-$mid-gate.log 2>&1
  gate_rc=$?
  sh probe/run.sh > probe/mut-$mid-jvm.log 2>&1
  jvm_rc=$?
  python3 probe/mutations.py restore "$mid" > probe/mut-$mid-restore.log 2>&1
  restore_rc=$?
  gate_ids=$(grep -oE '^[[:space:]]+([BCFK][0-9]+)' probe/mut-$mid-gate.log | tr -d ' ' | sort -u | tr '\n' ',' )
  red_tests=$(grep -oE '^[0-9]+\) [a-zA-Z]+' probe/jvm/junit.log | sed 's/^[0-9]*) //' | sort -u | tr '\n' ',')
  echo "$mid gate_exit=$gate_rc ids=[$gate_ids] jvm_exit=$jvm_rc red_tests=[$red_tests] restore_rc=$restore_rc" >> probe/mutation-results.txt
done
cat probe/mutation-results.txt
