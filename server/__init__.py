"""
Aztea HTTP application package.

``uvicorn server:app`` loads this package; implementation lives in ``server.application``.
Integration tests that monkeypatch globals should use ``import server.application as server``
so patches apply to the implementation module. ``uvicorn server:app`` continues to use
this package's ``app`` export.
"""

from __future__ import annotations

from typing import Any

import server.application as _application

app = _application.app


def __getattr__(name: str) -> Any:
    return getattr(_application, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_application)))
