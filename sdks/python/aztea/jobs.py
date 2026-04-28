from __future__ import annotations

from ._shim import load_module

_mod = load_module("jobs")

Job = _mod.Job
JobsNamespace = _mod.JobsNamespace
TERMINAL_JOB_STATUSES = _mod.TERMINAL_JOB_STATUSES

