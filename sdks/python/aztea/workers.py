from __future__ import annotations

from ._shim import load_module

_mod = load_module("workers")

JobSource = _mod.JobSource
PollingJobSource = _mod.PollingJobSource
WorkerFunction = _mod.WorkerFunction
WorkerRunner = _mod.WorkerRunner
build_worker_decorator = _mod.build_worker_decorator
