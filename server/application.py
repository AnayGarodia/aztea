"""FastAPI HTTP application entrypoint.

This module is the stable import target (`import server.application as server`) used
throughout tests and runtime monkeypatching. Implementation is sharded into ordered
files under `server/application_parts/` and loaded into this module namespace.
"""

from __future__ import annotations

from pathlib import Path

_PARTS_DIR = Path(__file__).resolve().with_name("application_parts")
if isinstance(__builtins__, dict):
    _EXECUTE = __builtins__.get("exec")
else:
    _EXECUTE = getattr(__builtins__, "exec", None)
if _EXECUTE is None:  # pragma: no cover - defensive safety
    raise RuntimeError("Python builtins.exec is unavailable")


def _part_paths() -> list[Path]:
    part_paths = sorted(_PARTS_DIR.glob("part_*.py"))
    if not part_paths:
        raise RuntimeError("server.application_parts has no part_*.py files")

    expected = [f"part_{idx:03d}.py" for idx in range(len(part_paths))]
    actual = [path.name for path in part_paths]
    if actual != expected:
        raise RuntimeError(
            "server.application_parts must be contiguous part_000..part_N files; "
            f"found {actual}"
        )
    return part_paths


def _load_application_parts() -> None:
    state = globals()
    for part_path in _part_paths():
        source = part_path.read_text(encoding="utf-8")
        code = compile(source, str(part_path), "exec")
        _EXECUTE(code, state, state)


_load_application_parts()

if "app" not in globals():  # pragma: no cover - defensive safety
    raise RuntimeError("server.application failed to initialize FastAPI app")
