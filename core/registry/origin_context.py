"""
origin_context.py — thread the ``jobs.origin`` value across an in-process call.

OWNS: a single ``ContextVar`` used by ``registry_auto_hire`` to mark "this
      delegated call originated from auto-hire" so the downstream
      ``registry_call`` → ``jobs.create_job`` chain can stamp the right origin
      without a signature change rippling through six call sites.
NOT OWNS: the validation of allowed origin values — that lives in
      ``core/jobs/crud.py::_validate_origin``.
INVARIANTS:
  - The contextvar is always read with ``get(None)`` so callers can default
    cleanly without touching the var directly.
  - ``use_origin`` is a context manager so the value is unset on exit even
    if the wrapped call raises.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_current_origin: ContextVar[str | None] = ContextVar(
    "aztea_job_origin", default=None,
)


def current_origin() -> str | None:
    """Return the active origin tag for this in-process call, or None if unset."""
    return _current_origin.get(None)


@contextmanager
def use_origin(origin: str | None) -> Iterator[None]:
    """Bind ``origin`` for the duration of the ``with`` block.

    Why: the auto-hire route delegates in-process to ``registry_call``, which
    creates the job. Passing origin via a contextvar avoids adding a kwarg
    to every intermediate function on the path.
    """
    token = _current_origin.set(origin)
    try:
        yield
    finally:
        _current_origin.reset(token)
