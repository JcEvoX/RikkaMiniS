#!/usr/bin/env python3
"""
Gate: FIX-H (deep-audit-1018 ISSUE-12a/b) — read the request body once, bound
the offload worker threads.

ISSUE-12a (ModelExecutionService.kt)
  The worker used to read `request.json` from disk THREE times — the queue-frame
  decision, the formal read and the ack-barrier decision — and parse it at each
  site, i.e. 2 redundant reads + 2 redundant parses of a body that can reach
  several MB (base64 input images). The rule: exactly ONE
  `requestFile.readText()` site, routed through `CapturedRequestBody.capture`,
  with the already-parsed body handed to both executors.

ISSUE-12b (NativeOffload.kt)
  The accept loop used to spawn one OS thread per accepted connection and let
  that thread park on the concurrency semaphore — a burst of N offloads created
  N threads (each ~1 MB native stack) while only `permits` made progress. The
  rule: the accept loop hands sockets to `BoundedWorkerPool` (no `thread(` in the
  loop, no `workerConcurrency.acquire()` anywhere in the file), and the pool
  spawns a worker only against the non-blocking `permits.tryAcquire()` bound and
  returns the permit when it drains empty.

Checks assert PRESENCE of the bounded/once-only routing rather than the absence
of a spelling, so a refactor that keeps the contract is a deliberate edit to
this gate rather than a silent pass.

Usage: python3 scripts/scan/modelsvc_single_read_check.py [repo_root]
       python3 scripts/scan/modelsvc_single_read_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

SERVICE_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelExecutionService.kt"
)
OFFLOAD_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/NativeOffload.kt"

READ_CALL = "requestFile.readText()"
CAPTURE_CALL = "CapturedRequestBody.capture { requestFile.readText() }"
SITE_PARSE = "JSONObject(requestFile.readText())"
OLD_LOCAL_RE = re.compile(r"\brequestText\b")
EXEC_SIGS = (
    "private fun executeRun(req: JSONObject, dir: File): String",
    "private fun executeStreamingRun(req: JSONObject, dir: File)",
)

POOL_SUBMIT = "pool.submit(client)"
OLD_ACQUIRE = "workerConcurrency.acquire()"
TRY_ACQUIRE = "permits.tryAcquire()"
PERMIT_RELEASE = "permits.release()"
POOL_DECL = "internal class BoundedWorkerPool"
CAP_CONST = "const val MAX_CONCURRENT_WORKERS = 2"
ACCEPT_LOOP = "private fun runAcceptLoop("
NEXT_MEMBER = "private fun handleClient("


def lines_of(src, needle):
    return [i + 1 for i, line in enumerate(src.split("\n")) if needle in line]


def check_service(src):
    v = []
    reads = lines_of(src, READ_CALL)
    if len(reads) != 1:
        where = ", ".join(str(n) for n in reads) or "-"
        v.append(
            "ModelExecutionService.kt:%s: %d x '%s' (exactly 1 expected — the "
            "body must be read once per run)" % (where, len(reads), READ_CALL)
        )
    if CAPTURE_CALL not in src:
        v.append(
            "ModelExecutionService.kt: the single read is not routed through "
            "'%s'" % CAPTURE_CALL
        )
    for n in lines_of(src, "requestText"):
        m = OLD_LOCAL_RE.search(src.split("\n")[n - 1])
        if m:
            v.append(
                "ModelExecutionService.kt:%d: leftover per-site local "
                "'requestText' (re-read path is back)" % n
            )
    if SITE_PARSE in src:
        v.append(
            "ModelExecutionService.kt:%d: per-site parse '%s' is back"
            % (lines_of(src, SITE_PARSE)[0], SITE_PARSE)
        )
    for sig in EXEC_SIGS:
        if sig not in src:
            v.append(
                "ModelExecutionService.kt: executor does not take the parsed "
                "body — missing '%s'" % sig
            )
    return v


def accept_loop_slice(src):
    start = src.find(ACCEPT_LOOP)
    if start < 0:
        return None
    end = src.find(NEXT_MEMBER, start)
    return src[start:] if end < 0 else src[start:end]


def check_offload(src):
    v = []
    loop = accept_loop_slice(src)
    if loop is None:
        v.append("NativeOffload.kt: 'runAcceptLoop' not found (ROT — update this gate)")
        return v
    for n in lines_of(loop, "thread("):
        v.append(
            "NativeOffload.kt: runAcceptLoop (line %d of the member) spawns a "
            "thread — accept must delegate to the bounded pool" % n
        )
    if POOL_SUBMIT not in loop:
        v.append(
            "NativeOffload.kt: runAcceptLoop does not hand clients to the "
            "bounded pool (missing '%s')" % POOL_SUBMIT
        )
    if OLD_ACQUIRE in src:
        v.append(
            "NativeOffload.kt:%d: '%s' is back (workers must not park as "
            "threads on the semaphore)" % (lines_of(src, OLD_ACQUIRE)[0], OLD_ACQUIRE)
        )
    if POOL_DECL not in src:
        v.append("NativeOffload.kt: '%s' missing" % POOL_DECL)
    if TRY_ACQUIRE not in src:
        v.append(
            "NativeOffload.kt: 'tryAcquire' bound missing — a submit could "
            "spawn an unbounded worker"
        )
    if PERMIT_RELEASE not in src:
        v.append("NativeOffload.kt: permit is never released on worker exit")
    if CAP_CONST not in src:
        v.append("NativeOffload.kt: default cap changed (expected '%s')" % CAP_CONST)
    return v


def read(root, rel):
    path = os.path.join(root, rel)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run(root):
    if not os.path.isdir(root):
        print("modelsvc_single_read_check: not a directory: %s" % root)
        return 2
    try:
        service = read(root, SERVICE_REL)
        offload = read(root, OFFLOAD_REL)
    except OSError as exc:
        print("modelsvc_single_read_check: cannot read sources: %s" % exc)
        return 2
    violations = check_service(service) + check_offload(offload)
    if violations:
        print("FAIL modelsvc_single_read_check")
        for v in violations:
            print("  " + v)
        return 1
    print("PASS modelsvc_single_read_check (1 read per run, capped worker spawns)")
    return 0


# --------------------------------------------------------------------------
# selftest: every criterion gets its own violating fixture, plus compliant ones
# --------------------------------------------------------------------------

GOOD_SERVICE = """
internal class ModelExecutionService {
    fun handle(dir: File, requestFile: File) {
        val requestBody = CapturedRequestBody.capture { requestFile.readText() }
        val wantsQueueFrames = requestBody.streamingFlagOrFalse()
        try {
            val req = requestBody.jsonOrThrow()
            if (req.optBoolean("streaming", false)) executeStreamingRun(req, dir) else executeRun(req, dir)
        } catch (t: Throwable) {}
        val isStreaming = requestBody.streamingFlagOrFalse()
    }

    private fun executeRun(req: JSONObject, dir: File): String { return "" }

    private fun executeStreamingRun(req: JSONObject, dir: File) {}
}
"""

BAD_SERVICE_SITE_READS = """
internal class ModelExecutionService {
    fun handle(dir: File, requestFile: File) {
        val wantsQueueFrames = runCatching {
            JSONObject(requestFile.readText()).optBoolean("streaming", false)
        }.getOrDefault(false)
        try {
            val requestText = requestFile.readText()
            val isStreaming = JSONObject(requestText).optBoolean("streaming", false)
            if (isStreaming) executeStreamingRun(requestText, dir) else executeRun(requestText, dir)
        } catch (t: Throwable) {}
        val isStreaming = runCatching {
            JSONObject(requestFile.readText()).optBoolean("streaming", false)
        }.getOrDefault(false)
    }

    private fun executeRun(requestJson: String, dir: File): String { return "" }

    private fun executeStreamingRun(requestJson: String, dir: File) {}
}
"""

BAD_SERVICE_NO_CAPTURE = GOOD_SERVICE.replace(
    CAPTURE_CALL, "requestFile.readText()"
).replace("requestBody.jsonOrThrow()", "JSONObject(requestFile.readText())")

GOOD_OFFLOAD = """
object NativeOffloadServer {
    internal const val MAX_CONCURRENT_WORKERS = 2

    @Volatile
    private var workerConcurrency = java.util.concurrent.Semaphore(MAX_CONCURRENT_WORKERS, true)

    private fun start() {
        val pool = BoundedWorkerPool<LocalSocket>(
            permits = workerConcurrency,
            threadName = "native-offload-worker",
            spawnThread = { name, body -> body() },
            handler = { client, queueMs -> handleClient(client, queueMs) },
        )
        thread(name = "native-offload-accept", isDaemon = true) { runAcceptLoop(s, pool) }
    }

    private fun runAcceptLoop(s: LocalServerSocket, pool: BoundedWorkerPool<LocalSocket>) {
        while (true) {
            val client = s.accept()
            pool.submit(client)
        }
    }

    private fun handleClient(client: LocalSocket, queueMs: Long) {}
}

internal class BoundedWorkerPool<T>(
    private val permits: java.util.concurrent.Semaphore,
    private val threadName: String,
    private val spawnThread: (name: String, body: () -> Unit) -> Unit,
    private val handler: (item: T, queueMs: Long) -> Unit,
) {
    fun submit(item: T) {
        val spawn = synchronized(lock) {
            pending.addLast(item)
            permits.tryAcquire()
        }
        if (spawn) spawnThread(threadName) { drain() }
    }

    private fun drain() {
        val entry = synchronized(lock) {
            pending.pollFirst() ?: run {
                permits.release()
                null
            }
        }
    }
}
"""

# pre-fix shape: per-connection thread that parks on the semaphore
BAD_OFFLOAD_PARKED = """
object NativeOffloadServer {
    internal const val MAX_CONCURRENT_WORKERS = 2
    private var workerConcurrency = java.util.concurrent.Semaphore(MAX_CONCURRENT_WORKERS, true)

    private fun start() {
        acceptThread = thread(name = "native-offload-accept", isDaemon = true) {
            runAcceptLoop(s)
        }
    }

    private fun runAcceptLoop(s: LocalServerSocket) {
        while (true) {
            val client = s.accept()
            thread(name = "native-offload-worker", isDaemon = true) {
                workerConcurrency.acquire()
                try {
                    handleClient(client, 0L)
                } finally {
                    workerConcurrency.release()
                }
            }
        }
    }

    private fun handleClient(client: LocalSocket, queueMs: Long) {}
}

internal class BoundedWorkerPool<T>(private val permits: java.util.concurrent.Semaphore) {
    fun submit(item: T) {
        permits.tryAcquire()
    }
}
"""

BAD_OFFLOAD_NO_BOUND = GOOD_OFFLOAD.replace(
    "            permits.tryAcquire()\n", ""
).replace(
    "                permits.release()\n", ""
).replace(
    "    private fun drain() {",
    "    private fun drain() {",
)


def selftest():
    failures = []

    def expect_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    def expect_trip(name, v, needle):
        if not any(needle in item for item in v):
            failures.append("%s did not trip '%s' (got %s)" % (name, needle, v))

    good_service = check_service(GOOD_SERVICE)
    good_offload = check_offload(GOOD_OFFLOAD)
    expect_clean("compliant service fixture", good_service)
    expect_clean("compliant offload fixture", good_offload)

    bad_reads = check_service(BAD_SERVICE_SITE_READS)
    expect_trip("three-read fixture (ISSUE-12a)", bad_reads, "exactly 1 expected")
    expect_trip("three-read fixture (ISSUE-12a)", bad_reads, "requestText")
    expect_trip("three-read fixture (ISSUE-12a)", bad_reads, "per-site parse")
    expect_trip("three-read fixture (ISSUE-12a)", bad_reads, "executeRun(req: JSONObject")

    bad_nocapture = check_service(BAD_SERVICE_NO_CAPTURE)
    expect_trip("no-capture fixture", bad_nocapture, "not routed through")

    bad_parked = check_offload(BAD_OFFLOAD_PARKED)
    expect_trip("parked-thread fixture (ISSUE-12b)", bad_parked, "spawns a thread")
    expect_trip("parked-thread fixture (ISSUE-12b)", bad_parked, "workerConcurrency.acquire()")
    expect_trip("parked-thread fixture (ISSUE-12b)", bad_parked, "does not hand clients")

    bad_nobound = check_offload(BAD_OFFLOAD_NO_BOUND)
    expect_trip("unbounded-submit fixture", bad_nobound, "tryAcquire")
    expect_trip("unbounded-submit fixture", bad_nobound, "never released")

    print("selftest compliant service      -> %d violation(s)" % len(good_service))
    print("selftest compliant offload      -> %d violation(s)" % len(good_offload))
    print("selftest three-read service     -> %d violation(s)" % len(bad_reads))
    print("selftest no-capture service     -> %d violation(s)" % len(bad_nocapture))
    print("selftest parked-thread offload  -> %d violation(s)" % len(bad_parked))
    print("selftest unbounded-submit       -> %d violation(s)" % len(bad_nobound))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("MODELSVC_SINGLE_READ_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())
