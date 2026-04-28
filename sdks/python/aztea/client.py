from __future__ import annotations

from ._shim import load_module

_mod = load_module("client")

AzteaClient = _mod.AzteaClient

