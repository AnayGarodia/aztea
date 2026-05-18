"""
executor_sandbox.py - Shared subprocess sandbox helpers.

The platform runs several "real tool" agents by spawning local binaries. The
main security invariant here is straightforward: spawned tools should receive a
minimal environment by default so host secrets do not leak into untrusted
programs. Callers can add explicit variables for their own workload, but the
baseline process environment is intentionally small and predictable.
"""

from __future__ import annotations

import os
from typing import Mapping

# ``HOME`` was dropped from the allowlist on 2026-05-18 after user code in
# ``multi_language_executor`` was observed echoing ``process.env`` and revealing
# ``HOME=/home/aztea`` to callers — leaking the platform service account name.
# ``PATH`` stays in the allowlist because subprocess launchers (bun, deno,
# node, go, etc.) rely on the child PATH to resolve relative binary names; the
# venv prefix that leaks alongside is a fixed deployment artifact rather than
# a per-session secret. If you add anything to this set, write down WHY in
# this comment block and verify the new variable cannot be used to fingerprint
# the host or carry forward a credential.
_SAFE_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


def build_subprocess_env(
    extra_env: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Return a minimal subprocess environment with optional caller overrides.

    Always sets ``LANG``/``LC_ALL`` so spawned tools have a stable locale. The
    parent ``HOME`` is dropped — anything that needs a writable home directory
    must be set explicitly via ``extra_env`` so the leak is auditable in code
    review.
    """
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    if "LC_ALL" not in env:
        env["LC_ALL"] = env["LANG"]
    if extra_env:
        for key, value in extra_env.items():
            env[str(key)] = str(value)
    return env
