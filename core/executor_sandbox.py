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
#
# 2026-05-18 (D12): we previously kept ``PATH`` in the allowlist, which leaked
# the venv prefix (``/home/aztea/app/venv/bin:...``) and revealed the platform
# service account name through a different channel. ``PATH`` is now REPLACED
# with a sanitised system default (see ``_SANITISED_PATH``) so subprocess
# launchers (bun, deno, node, go, etc.) can still resolve relative binary
# names but the venv path never reaches the child. The sanitised default
# covers the standard system bin paths on Debian/Ubuntu workers. Anything
# that needs a custom PATH must set it explicitly via ``extra_env`` so the
# new value is auditable in code review.
#
# If you add anything to this set, write down WHY in this comment block and
# verify the new variable cannot be used to fingerprint the host or carry
# forward a credential.
_SAFE_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}

# Sanitised PATH baked in regardless of the parent env. Covers the standard
# Debian/Ubuntu worker layout; sandbox scripts that need bun / deno / node
# can rely on these being on $PATH inside the worker image without the
# venv prefix leak the previous allowlist enabled.
_SANITISED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def build_subprocess_env(
    extra_env: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Return a minimal subprocess environment with optional caller overrides.

    Always sets ``LANG``/``LC_ALL`` so spawned tools have a stable locale and
    a sanitised ``PATH`` so subprocess launchers can resolve relative binary
    names without leaking the parent's venv prefix. The parent ``HOME`` is
    dropped — anything that needs a writable home directory must be set
    explicitly via ``extra_env`` so the leak is auditable in code review.
    """
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    if "LC_ALL" not in env:
        env["LC_ALL"] = env["LANG"]
    # 2026-05-18 (D12): PATH is always the sanitised default unless the
    # caller explicitly overrides via extra_env. This kills the venv-prefix
    # leak (``/home/aztea/app/venv/bin:...``) that ``process.env`` dumps
    # in multi_language_executor / python_code_executor exposed.
    env["PATH"] = _SANITISED_PATH
    if extra_env:
        for key, value in extra_env.items():
            env[str(key)] = str(value)
    return env
