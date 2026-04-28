from __future__ import annotations

from ._shim import load_module

_mod = load_module("types")

JSONPrimitive = _mod.JSONPrimitive
JSONValue = _mod.JSONValue
JSONObject = _mod.JSONObject
MessageType = _mod.MessageType

