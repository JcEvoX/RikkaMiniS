#!/usr/bin/env python3
"""ci_a1.py — [dv20-a1] CI 取数器（带 TLS 重试 + 两跳无 token 日志）。

用法：
  python3 ci_a1.py status              # 本分支的 run 列表（号/id/sha/状态/结论）
  python3 ci_a1.py job  <run_id>       # run 的 job（id/结论/step 概览）
  python3 ci_a1.py log  <job_id> <out> # 落 job 日志原文（两跳，二跳不带 auth）

由来：本机轮换代理会把 curl 间歇降级成明文 HTTP（rc=0 但正文是 HTML），
且 urllib 撞 TLS EOF 抖动 —— 取数一律走 urllib + 内容级校验 + 重试。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = "logicflow-GYW/RikkaMinis"
BRANCH = "fix/dv20-a1-backup-voice"
TOK = os.environ.get("GITHUB_TOKEN", "")
UA = {"Authorization": "Bearer " + TOK,
      "Accept": "application/vnd.github+json",
      "User-Agent": "minis"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def get(url, auth=True, timeout=40, tries=6):
    """Authenticated GET with retry; returns parsed JSON."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=(UA if auth else
                                                       {"User-Agent": "minis"}))
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as e:  # TLS EOF / 502 / EPIPE are transient here
            last = e
            time.sleep(5 + 5 * i)
    raise SystemExit("GET failed %s: %s" % (url, last))


def get_bytes(url, tries=6):
    """Unauthenticated GET of a pre-signed log URL, content-level validated."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "minis"})
            body = urllib.request.urlopen(req, timeout=120).read()
            if body[:1] == b"<":
                raise ValueError("got HTML (%s) — plain-HTTP downgrade" % body[:60])
            return body
        except Exception as e:
            last = e
            time.sleep(5 + 5 * i)
    raise SystemExit("log fetch failed %s: %s" % (url, last))


def status():
    d = get("https://api.github.com/repos/%s/actions/runs?branch=%s&per_page=10"
            % (REPO, urllib.request.quote(BRANCH)))
    print("total_count=%s" % d.get("total_count"))
    for r in d.get("workflow_runs", []):
        print("#%s id=%s %s %-10s %-9s head=%s attempt=%s %s"
              % (r.get("run_number"), r.get("id"), r.get("name"), r.get("status"),
                 r.get("conclusion"), (r.get("head_sha") or "")[:10],
                 r.get("run_attempt"), r.get("created_at")))


def job(run_id):
    d = get("https://api.github.com/repos/%s/actions/runs/%s/jobs" % (REPO, run_id))
    for j in d.get("jobs", []):
        print("job=%s %s steps=%s/%s urls.log=%s"
              % (j.get("id"), j.get("status"),
                 sum(1 for s in j.get("steps", []) if s.get("conclusion") == "success"),
                 len(j.get("steps", [])), j.get("urls", {}).get("logs")))
        bad = [s.get("name") for s in j.get("steps", [])
               if s.get("conclusion") in ("failure", "cancelled", "timed_out")]
        skipped = [s.get("name") for s in j.get("steps", [])
                   if s.get("conclusion") == "skipped"]
        print("  failed=%s skipped=%s" % (bad or "[]", skipped or "[]"))


def log(job_id, out):
    """Two hops: the authenticated endpoint 302s to a host that refuses the
    bearer token, so the second hop must be token-free."""
    opener = urllib.request.build_opener(_NoRedirect)
    loc = None
    for i in range(6):
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/actions/jobs/%s/logs" % (REPO, job_id),
                headers=UA)
            opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                break
            time.sleep(5 + 5 * i)
    if not loc:
        raise SystemExit("no redirect Location for job %s" % job_id)
    body = get_bytes(loc)
    with open(out, "wb") as fh:
        fh.write(body)
    print("wrote %s (%d bytes)" % (out, len(body)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "job":
        job(sys.argv[2])
    elif cmd == "log":
        log(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)
