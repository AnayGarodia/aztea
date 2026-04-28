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

_SAFE_ENV_KEYS = {
    "HOME",
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
    """Return a minimal subprocess environment with optional caller overrides."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_KEYS
    }
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    if "LC_ALL" not in env:
        env["LC_ALL"] = env["LANG"]
    if extra_env:
        for key, value in extra_env.items():
            env[str(key)] = str(value)
    return env
