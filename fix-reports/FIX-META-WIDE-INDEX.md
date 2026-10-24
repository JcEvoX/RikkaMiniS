# FIX-META-WIDE 12-misc-b ｜ 六门见证加固收口索引

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
结论：**20 条外部见证，修复前 20/20 盲（rc=0），修复后 20/20 CAUGHT（rc=1，restore 全 True）**。
零生产代码改动；冻结面（`scan.sh` / `test_scan.py`）、`backlog.md`、`credibility.md` 未触碰。

## CI

| run | 结论 | head_sha | job |
|---|---|---|---|
| `40134937808`（#2634） | success（attempt 1） | `64582201877c1849ca878a1a6e712c4e3d5549d5` | `120994351104` |

判读：`Summary: 112 passed, 0 failed` 恰一条 ｜ `ALL 344 CASES PASS` 恰一条 ｜ ` e: ` = 0 ｜
唯一 skipped = `Publish to Releases`（main 门控）。

## 六门

| 门 | 见证 | 轴 | 自带臂 | CI 门头 | 报告 |
|---|---|---|---|---|---|
| `deeplink_settings_routes_check` | 4 | spelling + dataflow | 见报告 §5 | `[64/112] F/ISSUE-06 deeplink settings routes` | [FIX-META-WIDE-deeplink_settings_routes_check.md](FIX-META-WIDE-deeplink_settings_routes_check.md) |
| `envvar_config_group_check` | 4 | dataflow | 见报告 §5 | `[103/112] envvar_config_group_check` | [FIX-META-WIDE-envvar_config_group_check.md](FIX-META-WIDE-envvar_config_group_check.md) |
| `openminis_compat_field_parity_check` | 3 | dataflow + order | 见报告 §5 | `[79/112] D/ISSUE-04 openminis compat field parity` | [FIX-META-WIDE-openminis_compat_field_parity_check.md](FIX-META-WIDE-openminis_compat_field_parity_check.md) |
| `subagent_handback_check` | 3 | dataflow + order | 见报告 §5 | `[31/112] Sub-agent turn hand-back guard` | [FIX-META-WIDE-subagent_handback_check.md](FIX-META-WIDE-subagent_handback_check.md) |
| `outcome_known_chain_check` | 3 | dataflow | 见报告 §5 | `[46/112] B/ISSUE-04 outcomeKnown chain` | [FIX-META-WIDE-outcome_known_chain_check.md](FIX-META-WIDE-outcome_known_chain_check.md) |
| `modelsvc_single_read_check` | 3 | spelling + dataflow | 见报告 §5 | `[51/112] H/ISSUE-12a/b modelservice single-read + worker cap` | [FIX-META-WIDE-modelsvc_single_read_check.md](FIX-META-WIDE-modelsvc_single_read_check.md) |

## 装置

- 见证解析：`/var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py`
  （从任务书 `task-12-misc-b.md` 的 find/replace 块解析出 20 条，逐条记录文件 + 原文）
- 反攻：`.../verify_witnesses.py <worktree> [--only GATE]` —— 打补丁 → 跑门 → 字节级复原
  （sha256 每次核对；`results-before.json` 20/20 BLIND、`results-after.json` 20/20 CAUGHT）
- 报告生成：`.../make_reports.py` / `.../backfill_ci.py`（围栏在运行期拼，绕开 file_write 吞反引号）

## 一句话给主会话

六门判据从「锚存在」改到「锚效果达成」（dataflow / spelling / order 三轴），每条见证都固化成
自带臂（红=失败），真树仍绿，分支 CI 绿；**未合并 main**，起手权在用户。
