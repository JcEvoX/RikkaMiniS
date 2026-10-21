#!/usr/bin/env python3
"""judge_a1.py — [dv20-a1] 分支 CI 判读（停止节点的唯一证据）

用法：
  python3 judge_a1.py run <run_id>      # 取元数据 + 落日志 + 逐条断言
  python3 judge_a1.py sample <logfile>  # 打印真样本形态（写判据前先 grep 真行）

判据按 §8.7 九条；三条新门留痕**只打印不硬断言句式**（教训 §1039-A4 R8：
selftest 原始 stdout 不进 CI 日志，判据须取自行文而非臆想句式）。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_a1  # noqa: E402

REPO = ci_a1.REPO
EXPECT_HEAD = "7a2705c111f7f456c7c7bc4d8fcfe7327d000d04"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci")
GATES = ("mcp_oauth_lifecycle_check", "envvar_config_group_check",
         "provider_rpc_symmetry_check")
TESTS = ("McpArtifactRedactionTest", "SystemVoiceGroupRoundTripTest")


def _get_json(url):
    return ci_a1.get(url)


def run(run_id):
    os.makedirs(OUT, exist_ok=True)
    meta = _get_json("https://api.github.com/repos/%s/actions/runs/%s" % (REPO, run_id))
    jobs = _get_json("https://api.github.com/repos/%s/actions/runs/%s/jobs?per_page=100"
                     % (REPO, run_id))
    with open(os.path.join(OUT, "run-%s.json" % run_id), "w") as fh:
        json.dump(meta, fh, indent=1)
    with open(os.path.join(OUT, "jobs-%s.json" % run_id), "w") as fh:
        json.dump(jobs, fh, indent=1)
    print("run #%s status=%s conclusion=%s head=%s branch=%s attempt=%s"
          % (meta.get("run_number"), meta.get("status"), meta.get("conclusion"),
             (meta.get("head_sha") or "")[:10], meta.get("head_branch"),
             meta.get("run_attempt")))
    if meta.get("status") != "completed":
        print("NOT COMPLETED YET — 停止节点未达成，稍后重跑")
        return 2
    jl = jobs.get("jobs", [])
    if not jl:
        raise SystemExit("no jobs for run %s" % run_id)
    job = jl[0]
    log_path = os.path.join(OUT, "job-%s.log" % job.get("id"))
    ci_a1.log(str(job.get("id")), log_path)
    return judge(meta, job, log_path)


def sample(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    print("--- Summary 行（全部） ---")
    for i, ln in enumerate(text.split("\n"), 1):
        if "Summary:" in ln:
            print("%d: %s" % (i, ln.strip()[:160]))
    print("--- 门头首末 ---")
    heads = re.findall(r"^\[(\d+)/(\d+)\]\s*(.+)$", text, re.M)
    print("count=%d first=%s last=%s" % (len(heads), heads[:1], heads[-1:]))
    print("--- 三门留痕 ---")
    for g in GATES:
        hits = [ln.strip()[:170] for ln in text.split("\n") if g in ln]
        print("%s -> %d 行" % (g, len(hits)))
        for h in hits[:6]:
            print("   ", h)
    print("--- 新测试类留痕 ---")
    for t in TESTS:
        hits = [ln.strip()[:170] for ln in text.split("\n") if t in ln]
        print("%s -> %d 行" % (t, len(hits)))
        for h in hits[:4]:
            print("   ", h)


def judge(meta, job, path):
    text = open(path, encoding="utf-8", errors="replace").read()
    lines = text.split("\n")
    res = []

    # 1 head_sha 三方一致（本地值写死，不从 CI 反推）
    res.append(("head_sha == 期望提交", meta.get("head_sha") == EXPECT_HEAD,
                meta.get("head_sha")))

    # 2 步级：失败/取消为空，唯一 skipped = Publish
    steps = job.get("steps", [])
    bad = [s.get("name") for s in steps
           if s.get("conclusion") in ("failure", "cancelled", "timed_out")]
    skipped = [s.get("name") for s in steps if s.get("conclusion") == "skipped"]
    res.append(("步级 failed/cancelled = 0", not bad, bad))
    res.append(("唯一 skipped = Publish（main 门控）",
                skipped in ([], ["Publish to Releases"]), skipped))
    res.append(("job 结论 success", job.get("conclusion") == "success", job.get("conclusion")))

    # 3 门头序号连续且互异 109
    heads = re.findall(r"^\[(\d+)/(\d+)\]\s*(\S.+?)\s*$", text, re.M)
    nums = [int(a) for a, _, _ in heads]
    names = set(n for _, _, n in heads)
    res.append(("门头 [1/109]..[109/109] 连续", nums == list(range(1, 110)),
                "count=%d unique=%d" % (len(nums), len(names))))
    res.append(("门名互异 = 109", len(names) == 109, len(names)))

    # 4 Summary：存在一条 109 passed 0 failed，且九条中无任何非零 failed
    summaries = [ln for ln in lines if "Summary:" in ln]
    has_full = any(re.search(r"Summary:\s*109 passed,\s*0 failed", s) for s in summaries)
    nonzero = [s.strip()[:120] for s in summaries
               if re.search(r"Summary:.*?(\d+) failed", s)
               and re.search(r"Summary:.*?(\d+) failed", s).group(1) != "0"]
    res.append(("Summary 109 passed / 0 failed", has_full, len(summaries)))
    res.append(("所有 Summary 无非零 failed", not nonzero, nonzero[:2]))

    # 5 夹具全量
    res.append(("ALL 336 CASES PASS", "ALL 336 CASES PASS" in text, None))

    # 6 编译错（精确形态 `Z e:`，防元数据行假阳性）
    kze = len(re.findall(r"Z e: ", text))
    res.append(("编译错 Z e: = 0", kze == 0, kze))

    # 7 游离 FAILED（不以 PASSED 结尾的 FAILED 行 = 真失败）
    stray = [ln.strip()[:150] for ln in lines
             if "FAILED" in ln and "PASSED" not in ln
             and not re.match(r"^\s*[\d+~-]+\s", ln.strip())]
    stray = [s for s in stray if "FAILED" in s and "test" in s.lower()]
    res.append(("游离 FAILED = 0", len(stray) == 0, stray[:2]))

    ok = all(x[1] for x in res)
    for name, passed, extra in res:
        print("%-46s %s  %s" % (name, "GREEN" if passed else "RED",
                                "" if extra is None else extra))
    print("--- 三门与新测试留痕（只作见证，不硬断言句式） ---")
    for g in GATES:
        hits = [ln.strip()[:150] for ln in lines if g in ln]
        print("%s -> %d 行" % (g, len(hits)), hits[:2])
    for t in TESTS:
        hits = [ln.strip()[:150] for ln in lines if t in ln]
        print("%s -> %d 行" % (t, len(hits)), hits[:2])
    print()
    print("CI JUDGE: %s (%d/%d)" % ("ALL GREEN" if ok else "RED",
                                    sum(1 for x in res if x[1]), len(res)))
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        sys.exit(run(sys.argv[2]))
    if cmd == "sample":
        sample(sys.argv[2])
    sys.exit(0)
