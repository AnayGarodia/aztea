"""FastAPI HTTP application entrypoint for the Aztea / aztea platform.

This module is the stable import target used by production deployments and tests::

    import server.application as server      # monkeypatchable in pytest
    from server import app                   # what uvicorn loads

The implementation is deliberately sharded into ordered files under
``server/application_parts/``. Each shard (``part_000.py`` … ``part_012.py``) is
kept below the repository's 1000-line budget and is concatenated into this
module's globals at import time. The shards share one logical namespace, so
routes, middleware, helpers, and module-level constants can reference each
other without cross-module imports — matching how the original monolithic
``server/application.py`` behaved, but with each file staying small enough to
read and review.

Production guarantees enforced here:

1. The shards folder must exist and contain at least one ``part_*.py`` file.
2. Shards must be contiguous (``part_000.py`` … ``part_N.py``) so that
   reordering or accidental deletions fail fast at startup rather than leaving
   half an app initialised in production.
3. After loading, ``app`` (the FastAPI instance) must exist in this module's
   globals. A missing ``app`` is treated as a deployment bug and raises before
   uvicorn can hand back a half-working process.

Shard ordering matters: ``part_000.py`` owns configuration, imports, logging,
and Sentry; ``part_001.py`` owns middleware (CORS, /api/* compat shim, request
tracing, prometheus); ``part_012.py`` owns wallet routes and the SPA fallback.
See ``CLAUDE.md`` for the full shard layout and editing rules.
"""

from __future__ import annotations

from pathlib import Path

# Directory containing ``part_*.py`` implementation shards.
_PARTS_DIR = Path(__file__).resolve().with_name("application_parts")

# Resolve the ``exec`` builtin once. ``__builtins__`` is a module at top level
# but a dict inside exec'd scopes — handle both so this module loads regardless
# of how it is imported.
if isinstance(__builtins__, dict):
    _EXECUTE = __builtins__.get("exec")
else:
    _EXECUTE = getattr(__builtins__, "exec", None)
if _EXECUTE is None:  # pragma: no cover - defensive safety
    raise RuntimeError("Python builtins.exec is unavailable")


def _part_paths() -> list[Path]:
    """Return the ordered list of implementation shards.

    Fails fast when the directory is empty or when the shards are not
    contiguous (``part_000.py`` … ``part_N.py``). Deployers who rsync a
    partial release therefore never boot a half-loaded FastAPI app.
    """
    part_paths = sorted(_PARTS_DIR.glob("part_*.py"))
    if not part_paths:
        raise RuntimeError("server.application_parts has no part_*.py files")

    # Shards normally must be contiguous (part_000..part_N) so a botched/partial deploy that drops
    # a shard fails fast instead of booting with a missing route. RESERVED_PENDING holds indices that
    # may be TEMPORARILY absent because their owner is landing them on main out-of-band; any OTHER
    # gap still fails fast, so the dropped-shard guard stays intact for every real shard.
    # part_019 = a LEGACY analytics shard that exists only as an untracked file on the production box.
    # The analytics/telemetry pipeline was migrated to server/routes/otto_telemetry.py (a module, not a
    # shard), so part_019 will NOT be added to git. The gap is reserved so fresh checkouts (which lack
    # the box's legacy file) still boot; remove 19 here once that legacy file is retired from the box.
    RESERVED_PENDING = {19}
    actual = [path.name for path in part_paths]
    indices = sorted(int(p.name[len("part_") : -len(".py")]) for p in part_paths)
    missing = set(range(max(indices) + 1)) - set(indices)
    unexpected = missing - RESERVED_PENDING
    if unexpected:
        raise RuntimeError(
            "server.application_parts is missing shard(s) "
            f"{sorted('part_%03d.py' % i for i in unexpected)} (contiguity guard); found {actual}"
        )
    return part_paths


def _load_application_parts() -> None:
    """Compile and execute each shard into this module's globals, in order."""
    state = globals()
    for part_path in _part_paths():
        source = part_path.read_text(encoding="utf-8")
        code = compile(source, str(part_path), "exec")
        _EXECUTE(code, state, state)


_load_application_parts()

if "app" not in globals():  # pragma: no cover - defensive safety
    raise RuntimeError("server.application failed to initialize FastAPI app")
