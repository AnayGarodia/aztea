from __future__ import annotations

from ._shim import load_module

_mod = load_module("async_client")

AsyncAzteaClient = _mod.AsyncAzteaClient

