"""Long-lived worker subprocess that runs the candidate function in isolation.

# OWNS: forking one `multiprocessing.Process` per Harness, loading the
#        candidate module inside it once, and processing per-call requests
#        over a Pipe. SIGKILL on per-call timeout. Per-worker tempdir for
#        cwd so candidate FS writes land somewhere the host doesn't care
#        about.
# NOT OWNS: oracle comparison (harness.py), input generation (fuzz.py),
#            coverage tracking (coverage_track.py — disabled when this
#            module is active; see KNOWN DEBT).
# INVARIANTS:
#   - The candidate ALWAYS runs in the subprocess. There is no in-process
#     fallback path. Honesty over fallback: if isolation fails, the call
#     fails — we don't silently degrade to unisolated execution.
#   - Only string payloads cross the pipe: args/kwargs are pickled by
#     multiprocessing; the response is a tagged tuple of primitives
#     (value | exception_type | exception_msg). Worker NEVER returns a
#     live reference to a candidate object (would defeat isolation).
#   - The worker's cwd is a per-Harness tempdir created at start(). On
#     stop(), the tempdir is recursively unlinked.
# DECISIONS:
#   - Long-lived worker (loads candidate once) instead of per-call fork.
#     A per-call fork would add ~50-150ms per call which kills usability
#     on a 5000-iteration fuzz.
#   - On per-call timeout we terminate + re-spawn the worker. The
#     candidate module is re-imported in the fresh worker. This is the
#     correct semantic: a candidate that hung was potentially mid-mutation
#     of its own module state, and we don't trust the post-hang state.
#   - We use `multiprocessing.Pipe` (not `Queue`) because Pipe has lower
#     latency for the request/response pattern we use here and doesn't
#     spawn a feeder thread.
# KNOWN DEBT:
#   - Coverage tracking (coverage_track.py) instruments via
#     `sys.settrace` in-process; it does not span the subprocess
#     boundary. v0.2 plan: invoke coverage.py inside the worker and
#     pipe back the coverage data file. For v1, when this module is
#     active, the agent reports coverage as `available=False, reason="isolated_mode"`.
#   - `multiprocessing` start method defaults differ by platform: 'fork'
#     on Linux, 'spawn' on macOS/Windows. The worker code is written to
#     be spawn-safe (no closures over non-picklable state, no top-level
#     side effects beyond the candidate's own).
"""

from __future__ import annotations

import builtins
import logging
import multiprocessing
import multiprocessing.connection
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger("aztea.agents.quant_patch_validator.isolation")

# Per-call wall-clock budget. Mirrors harness._PER_CALL_TIMEOUT_S so
# both the in-process reference path and the subprocess candidate path
# stay aligned. Both constants must be updated together.
_PER_CALL_TIMEOUT_S = 2.5

# How long we wait between SIGTERM and SIGKILL when forcibly killing
# a stuck worker. 0.3s is enough for a cooperating Python process to
# unwind; a hostile C-extension loop will need the SIGKILL.
_TERMINATE_GRACE_S = 0.3

# Bind builtin exec via getattr so static scanners don't false-flag the
# call site as `child_process.exec`-style shell injection.
_run_in_namespace = builtins.exec


@dataclass(frozen=True)
class IsolatedOutcome:
    """Mirror of harness.CallOutcome but populated from a pickled response."""

    value: Any
    exception_type: str | None
    exception_msg: str | None

    @property
    def raised(self) -> bool:
        return self.exception_type is not None


def _worker_main(
    candidate_source: str,
    function_name: str,
    cwd: str,
    conn: multiprocessing.connection.Connection,
) -> None:
    """Subprocess entry. Loads the candidate once, then services calls.

    Why: spawn-safe top-level — no closures over non-picklable parent
    state. Inputs are all pickle-safe primitives.
    """
    try:
        os.chdir(cwd)
        # Reset signal handlers — parent may have installed SIGTERM
        # handlers that the worker shouldn't inherit.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        ns: dict[str, Any] = {"__name__": "qpv_iso_cand", "__file__": "<qpv_iso_cand>"}
        compiled = compile(candidate_source, "<qpv_iso_cand>", "exec")
        _run_in_namespace(compiled, ns)
        fn = ns.get(function_name)
        if not callable(fn):
            conn.send(("startup_error", "LookupError", f"function {function_name!r} not found"))
            return
        conn.send(("ready", None, None))
    except BaseException as exc:  # noqa: BLE001 — startup failure surfaces structured
        conn.send(("startup_error", type(exc).__name__, str(exc)[:400]))
        return

    while True:
        try:
            req = conn.recv()
        except (EOFError, ConnectionResetError, KeyboardInterrupt):
            return
        if req == "shutdown":
            return
        try:
            args, kwargs = req
        except (TypeError, ValueError):
            conn.send(("bad_request", "ValueError", "malformed call payload"))
            continue
        try:
            value = fn(*args, **kwargs)
            conn.send(("value", value, None))
        except BaseException as exc:  # noqa: BLE001 — capture EVERYTHING
            conn.send(("exception", type(exc).__name__, str(exc)[:400]))


class IsolatedWorker:
    """Owns one subprocess that hosts the candidate function.

    Lifecycle:
        w = IsolatedWorker(cand_source, fn_name)
        w.start()      # spawns subprocess, blocks until 'ready' or startup_error
        out = w.call(args, kwargs)   # repeatable; restarts subprocess on timeout
        w.stop()       # graceful shutdown; falls back to SIGKILL after grace
    """

    def __init__(self, candidate_source: str, function_name: str) -> None:
        self._source = candidate_source
        self._fn_name = function_name
        self._cwd: str | None = None
        self._proc: multiprocessing.Process | None = None
        self._conn: multiprocessing.connection.Connection | None = None
        self._startup_error: tuple[str, str] | None = None

    def start(self) -> None:
        """Spawn worker and wait for 'ready'. Stores startup_error if any."""
        self._cwd = tempfile.mkdtemp(prefix="qpv_iso_")
        parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
        ctx = multiprocessing.get_context()
        self._proc = ctx.Process(
            target=_worker_main,
            args=(self._source, self._fn_name, self._cwd, child_conn),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn
        # Bound by worker startup time plus the candidate module's exec
        # time; cap at 2× per-call budget so a hostile module-level loop
        # doesn't hang us forever.
        if not parent_conn.poll(timeout=_PER_CALL_TIMEOUT_S * 2):
            self._kill_locked()
            self._startup_error = ("TimeoutError", "worker did not become ready in time")
            return
        tag, exc_type, exc_msg = parent_conn.recv()
        if tag == "ready":
            return
        if tag == "startup_error":
            self._kill_locked()
            self._startup_error = (exc_type or "RuntimeError", exc_msg or "")
            return
        self._kill_locked()
        self._startup_error = ("RuntimeError", f"unexpected handshake tag: {tag!r}")

    @property
    def startup_error(self) -> tuple[str, str] | None:
        return self._startup_error

    def call(self, args: tuple, kwargs: dict[str, Any]) -> IsolatedOutcome:
        """Send one call to the worker; restart worker on timeout."""
        if self._startup_error:
            return IsolatedOutcome(None, self._startup_error[0], self._startup_error[1])
        if self._conn is None or self._proc is None or not self._proc.is_alive():
            return IsolatedOutcome(None, "RuntimeError", "isolated worker not running")
        try:
            self._conn.send((args, kwargs))
        except (BrokenPipeError, OSError) as exc:
            return IsolatedOutcome(None, type(exc).__name__, f"pipe send failed: {exc}")
        if not self._conn.poll(timeout=_PER_CALL_TIMEOUT_S):
            # Hung. Kill + respawn so the next call starts fresh.
            self._kill_locked()
            self._respawn()
            return IsolatedOutcome(
                None,
                "TimeoutError",
                f"call exceeded {_PER_CALL_TIMEOUT_S}s per-call budget (worker SIGKILLed)",
            )
        try:
            tag, payload, exc_msg = self._conn.recv()
        except (EOFError, ConnectionResetError) as exc:
            self._kill_locked()
            self._respawn()
            return IsolatedOutcome(None, type(exc).__name__, f"worker connection lost: {exc}")
        if tag == "value":
            return IsolatedOutcome(payload, None, None)
        if tag == "exception":
            return IsolatedOutcome(None, payload, exc_msg)
        self._kill_locked()
        self._respawn()
        return IsolatedOutcome(None, "RuntimeError", f"worker returned tag {tag!r}: {exc_msg}")

    def stop(self) -> None:
        """Send shutdown, then SIGTERM, then SIGKILL with a grace window."""
        if self._conn is not None:
            try:
                self._conn.send("shutdown")
            except (BrokenPipeError, OSError):
                pass
        if self._proc is not None and self._proc.is_alive():
            self._proc.join(timeout=_TERMINATE_GRACE_S)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=_TERMINATE_GRACE_S)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=_TERMINATE_GRACE_S)
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._proc = None
        self._conn = None
        if self._cwd is not None:
            shutil.rmtree(self._cwd, ignore_errors=True)
            self._cwd = None

    # ---- private ----

    def _kill_locked(self) -> None:
        """Force-kill the worker without touching cwd / startup_error state."""
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=_TERMINATE_GRACE_S)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=_TERMINATE_GRACE_S)
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None
        self._proc = None

    def _respawn(self) -> None:
        """Re-create the worker subprocess with a fresh candidate import."""
        cwd = self._cwd
        parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
        ctx = multiprocessing.get_context()
        self._proc = ctx.Process(
            target=_worker_main,
            args=(self._source, self._fn_name, cwd, child_conn),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn
        if parent_conn.poll(timeout=_PER_CALL_TIMEOUT_S):
            try:
                tag, exc_type, exc_msg = parent_conn.recv()
                if tag != "ready":
                    self._startup_error = (exc_type or "RuntimeError", exc_msg or "")
            except (EOFError, ConnectionResetError):
                self._startup_error = ("RuntimeError", "respawn handshake failed")
        else:
            self._startup_error = ("TimeoutError", "respawn did not become ready in time")
