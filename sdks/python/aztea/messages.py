from __future__ import annotations

from ._shim import load_module

_mod = load_module("messages")

ask_clarification = _mod.ask_clarification
answer_clarification = _mod.answer_clarification
send_progress = _mod.send_progress
send_partial_result = _mod.send_partial_result
send_note = _mod.send_note

