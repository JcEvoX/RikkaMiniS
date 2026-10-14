"""STDIO MCP transport — spawn a child process, talk JSON-RPC over stdin/stdout.

Flow: spawn -> initialize handshake -> tools/list / tools/call. Each line on the
child's stdout is one JSON-RPC message (newline-delimited JSON, the MCP stdio
convention). Crash (non-zero exit / broken pipe before a reply) triggers ONE
automatic retry. 5-minute overall timeout per call. Each spawned child gets ONE
resident reader thread that readline()s stdout and dispatches replies by
msg["id"] to per-request queues; a timeout only abandons the waiter, never the
reader, so a stale per-request thread can never steal a later reply. Child
stderr is drained into a ring buffer and attached to crash errors.

`$ENV_VAR` references in env values are expanded from the parent environment.
Missing commands (npx/uvx/...) are auto-resolved via utils.deps first.
"""

import collections
import json
import os
import queue
import subprocess
import threading

from transport.http import MCPError, expand_env  # reuse error type + expander
from utils import deps

TIMEOUT_SECONDS = 300  # 5 min
STDERR_RING_LINES = 40  # child stderr lines kept for crash context


class _ReplyTimeout(Exception):
    """Raised by _StdioReader.wait when no reply arrives within the timeout."""


class _ChildEOF(Exception):
    """Raised by _StdioReader.wait when the child's stdout hit EOF."""


class _StdioReader:
    """One resident reader thread per spawned child process.

    Reads newline-delimited JSON with readline() (never for-iter: iteration
    pulls C-level read-ahead chunks that a later, fresh reader thread cannot
    reliably pick up) and dispatches each reply by msg["id"] to a per-request
    queue. A timeout only abandons the waiter — the reader keeps running, so a
    late reply lands in its queue or is dropped, but can never be consumed by
    a stale per-request thread racing on the same pipe. EOF fails all pending
    waiters. Child stderr is drained on a second thread into a ring buffer so
    crash errors can carry the last lines (stderr=PIPE without a drain thread
    would deadlock a chatty child on a full pipe).
    """

    def __init__(self, proc):
        self.proc = proc
        self._pending = {}          # msg id -> queue.Queue (1-slot)
        self._pending_lock = threading.Lock()
        self._eof = False
        self._stderr_lines = collections.deque(maxlen=STDERR_RING_LINES)
        self._stderr_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _loop(self):
        try:
            while True:
                raw = self.proc.stdout.readline()
                if not raw:  # EOF
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                box = self._pending.get(msg.get("id"))
                if box is not None:
                    box.put(msg)
                # replies with no waiter (late, unsolicited, notifications)
                # are dropped — same as the old reader ignoring non-matching
                # ids
        except (OSError, ValueError):
            pass
        finally:
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
                self._eof = True
            for box in pending:
                box.put(None)  # EOF sentinel: fail every pending waiter

    def _drain_stderr(self):
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def register(self, want_id):
        """[rv1014-C1] Register the pending box for want_id BEFORE the request
        is written to the pipe (register-then-send). A reply that arrives
        while the requester sits between _send() and wait() then lands in
        this box instead of being dropped by the reader loop (no waiter yet
        → drop → timeout + retry that can re-execute a side-effecting tool).
        Raises _ChildEOF when stdout already hit EOF."""
        box = queue.Queue()
        with self._pending_lock:
            if self._eof:
                raise _ChildEOF()
            self._pending[want_id] = box
        return box

    def cancel(self, want_id):
        """Drop a registration whose request was never sent (send failure);
        keeps a failed _rpc from leaving a stale box in _pending."""
        with self._pending_lock:
            self._pending.pop(want_id, None)

    def wait(self, want_id, timeout, box=None):
        """Wait for the reply to want_id. When [box] is given it must already
        be registered (see register(); register-then-send, [rv1014-C1]);
        otherwise a box is registered here. Raises _ReplyTimeout on timeout
        (the reader keeps running) and _ChildEOF when stdout has hit EOF."""
        if box is None:
            box = queue.Queue()
            with self._pending_lock:
                if self._eof:
                    raise _ChildEOF()
                self._pending[want_id] = box
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise _ReplyTimeout()
        finally:
            with self._pending_lock:
                self._pending.pop(want_id, None)
        if msg is None:
            raise _ChildEOF()
        return msg

    def stderr_tail(self):
        """Last stderr lines for crash context; empty string when none."""
        with self._stderr_lock:
            lines = list(self._stderr_lines)
        if not lines:
            return ""
        return "\nchild stderr (last %d lines):\n%s" % (
            len(lines), "\n".join(lines))


def _expand_env_map(env):
    merged = dict(os.environ)
    for k, v in (env or {}).items():
        merged[k] = expand_env(v)
    return merged


class STDIOTransport:
    def __init__(self, server, server_name):
        self.command = server.get("command", "")
        self.args = server.get("args", []) or []
        self.env = _expand_env_map(server.get("env"))
        self.server_name = server_name
        self._id = 0
        self._readers = {}  # proc -> _StdioReader (one resident reader each)

    def _next_id(self):
        self._id += 1
        return self._id

    def _spawn(self):
        deps.ensure_command(self.command)
        try:
            proc = subprocess.Popen(
                [self.command] + list(self.args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise MCPError("CONNECTION_ERROR", "command not found: %s" % self.command)
        except OSError as exc:
            raise MCPError("CONNECTION_ERROR", str(exc))
        # One resident reader thread per child: dispatches by id, drains
        # stderr, survives timeouts (unlike the old per-request thread).
        self._readers[proc] = _StdioReader(proc)
        return proc

    def _stderr_tail(self, proc):
        reader = self._readers.get(proc)
        return reader.stderr_tail() if reader else ""

    def _send(self, proc, method, params=None, notify=False, want_id=None):
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            # [rv1014-C1] want_id may be preallocated by _rpc (register-then-
            # send); fall back to allocating here for notify-less direct calls.
            body["id"] = self._next_id() if want_id is None else want_id
        if params is not None:
            body["params"] = params
        try:
            proc.stdin.write(json.dumps(body) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise MCPError(
                "STDIO_CRASH",
                "child closed stdin%s" % self._stderr_tail(proc))
        return body.get("id")

    def _read_reply(self, proc, want_id, box=None):
        """Read the reply for want_id from the resident reader. The reader
        keeps running on timeout, so this never leaves a stale thread racing
        on the pipe. Raises STDIO_CRASH on EOF, TIMEOUT on the watchdog.
        [box] must already be registered when given (register-then-send)."""
        reader = self._readers.get(proc)
        if reader is None:
            raise MCPError("STDIO_CRASH", "child exited before replying")
        try:
            return reader.wait(want_id, TIMEOUT_SECONDS, box=box)
        except _ReplyTimeout:
            raise MCPError("TIMEOUT", "no reply after %ds" % TIMEOUT_SECONDS)
        except _ChildEOF:
            raise MCPError(
                "STDIO_CRASH",
                "child exited before replying%s" % self._stderr_tail(proc))

    def _rpc(self, proc, method, params=None):
        # [rv1014-C1] register-then-send: the pending box MUST be registered
        # before the request hits the pipe. The old order (_send flush, then
        # _read_reply registers) had a preemption window — a reply arriving
        # before the registration found no waiter and was dropped by the
        # reader, turning into a timeout + retry that can re-run a
        # side-effecting tool.
        want_id = self._next_id()
        reader = self._readers.get(proc)
        if reader is None:
            raise MCPError("STDIO_CRASH", "child exited before replying")
        try:
            box = reader.register(want_id)
        except _ChildEOF:
            raise MCPError(
                "STDIO_CRASH",
                "child exited before replying%s" % self._stderr_tail(proc))
        try:
            self._send(proc, method, params, want_id=want_id)
        except BaseException:
            reader.cancel(want_id)
            raise
        reply = self._read_reply(proc, want_id, box=box)
        if reply.get("error"):
            err = reply["error"]
            raise MCPError("MCP_ERROR", err.get("message", json.dumps(err)))
        return reply.get("result")

    def _handshake(self, proc):
        result = self._rpc(
            proc,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "minis-mcp-cli", "version": "1.0.0"},
            },
        )
        self._send(proc, "notifications/initialized", notify=True)
        return result

    def _session(self, fn):
        """Run fn(proc) inside one spawned child, with a single crash retry."""
        last_err = None
        for attempt in range(2):
            self._id = 0
            proc = self._spawn()
            try:
                self._handshake(proc)
                return fn(proc)
            except MCPError as exc:
                last_err = exc
                if exc.code != "STDIO_CRASH" or attempt == 1:
                    raise
                # else: retry once on a crash
            finally:
                self._readers.pop(proc, None)
                try:
                    proc.terminate()
                except OSError:
                    pass
        if last_err:
            raise last_err

    def list_tools(self):
        return self._session(
            lambda proc: (self._rpc(proc, "tools/list") or {}).get("tools", [])
        )

    def call_tool(self, tool, arguments):
        return self._session(
            lambda proc: self._rpc(
                proc, "tools/call", {"name": tool, "arguments": arguments or {}}
            )
        )

    def ping(self):
        return self._session(lambda proc: True)
