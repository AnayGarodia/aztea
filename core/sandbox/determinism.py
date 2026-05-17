"""Determinism levers: frozen clock, seeded RNG, pinned image digest verification.

# OWNS: assembling the env vars + docker-run flags that pin a sandbox to a
#       deterministic clock + RNG; verifying user-supplied image digests.
# NOT OWNS: image build/pull (lives in boot.py), actual env application.
# INVARIANTS:
#   * If the caller asks for ``frozen_at`` but libfaketime isn't available
#     on the host, we surface ``clock_freeze_supported: False`` in the boot
#     response — we do NOT silently ignore the request.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from pathlib import Path
from typing import Any

from core.sandbox.models import SandboxInvalidInput

_FAKETIME_LIB_CANDIDATES = (
    "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/local/lib/faketime/libfaketime.so.1",
    "/opt/homebrew/lib/faketime/libfaketime.dylib.1",
    "/usr/local/lib/faketime/libfaketime.dylib.1",
)
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def determinism_env(clock_cfg: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, Any]]:
    """Return ``(env_vars, status_block)`` for the requested clock policy.

    Why: clock freezing has two pieces — the env vars (``FAKETIME``,
    ``LD_PRELOAD`` / ``DYLD_INSERT_LIBRARIES``) that injected libfaketime
    reads, and a status block surfaced in the boot response so callers
    can audit whether the freeze took effect.
    """
    cfg = clock_cfg or {}
    frozen_at = cfg.get("frozen_at")
    rate = cfg.get("rate")
    seed_env = {"AZTEA_SANDBOX_SEED": str(cfg.get("rng_seed") or "")}
    if not frozen_at and rate in (None, 1.0):
        return seed_env, {"clock_frozen": False, "rate": 1.0}
    if frozen_at is not None and not _ISO8601_RE.match(str(frozen_at)):
        raise SandboxInvalidInput(
            f"clock.frozen_at must be ISO 8601 ('YYYY-MM-DDThh:mm:ssZ'); got {frozen_at!r}"
        )
    lib_path = _faketime_lib_path()
    base_env: dict[str, str] = {}
    if frozen_at:
        base_env["FAKETIME"] = _to_faketime_format(str(frozen_at))
    if rate not in (None, 1.0):
        try:
            base_env["FAKETIME_NO_CACHE"] = "1"
            base_env["FAKETIME_RATE"] = str(float(rate))
        except (TypeError, ValueError):
            raise SandboxInvalidInput(f"clock.rate must be numeric; got {rate!r}") from None
    if lib_path:
        base_env["LD_PRELOAD"] = lib_path
        base_env["DYLD_INSERT_LIBRARIES"] = lib_path
        status = {
            "clock_frozen": bool(frozen_at),
            "rate": float(rate or 1.0),
            "library": lib_path,
            "clock_freeze_supported": True,
        }
    else:
        status = {
            "clock_frozen": bool(frozen_at),
            "rate": float(rate or 1.0),
            "library": None,
            "clock_freeze_supported": False,
            "note": (
                "libfaketime not found on host; FAKETIME env was set but "
                "containers must include libfaketime in their image to apply "
                "the freeze. Install libfaketime on the host or bake it into "
                "the image."
            ),
        }
    return {**base_env, **seed_env}, status


def verify_image_digest(image_ref: str, expected_digest: str) -> bool:
    """Pure-ish: ``True`` iff ``image_ref`` includes the expected ``sha256:...`` digest.

    Why: the spec asks for pinned base images verified by digest; the
    simplest enforcement (and the one that doesn't require pulling) is
    insisting the user supply ``image@sha256:<hex>`` style refs.
    """
    if not image_ref or not expected_digest:
        return False
    if "@" not in image_ref:
        return False
    _, supplied = image_ref.rsplit("@", 1)
    return supplied.lower() == expected_digest.lower()


def _faketime_lib_path() -> str | None:
    """Side-effect: probe well-known locations for libfaketime; ``None`` if absent."""
    explicit = os.environ.get("AZTEA_SANDBOX_FAKETIME_LIB")
    if explicit and Path(explicit).is_file():
        return explicit
    for candidate in _FAKETIME_LIB_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    # On macOS / Linux dev boxes libfaketime may ship under brew's prefix.
    brew_prefix = shutil.which("brew")
    if brew_prefix:
        guess = Path("/opt/homebrew/lib/faketime/libfaketime.dylib.1")
        if guess.is_file():
            return str(guess)
    return None


def _to_faketime_format(iso: str) -> str:
    """Pure: ISO8601 → ``@YYYY-MM-DD hh:mm:ss`` format libfaketime understands."""
    parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return parsed.strftime("@%Y-%m-%d %H:%M:%S")
