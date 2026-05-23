"""
runners — parallel runner pool for fan-out reasoning agents (B8, A1, etc.).

Public surface:
    FanOutRunner — orchestrator
    FanOutSpec, WorkerResult, AggregatedResult — typed I/O
    FailurePolicy, AggregatedStatus — enums
"""

from __future__ import annotations

from core.runners.dispatch import (
    InProcessBackend,
    JobLifecycleBackend,
    default_backend,
)
from core.runners.pool import FanOutRunner
from core.runners.types import (
    AggregatedResult,
    AggregatedStatus,
    FailurePolicy,
    FanOutSpec,
    WorkerResult,
)

__all__ = [
    "FanOutRunner",
    "FanOutSpec",
    "WorkerResult",
    "AggregatedResult",
    "FailurePolicy",
    "AggregatedStatus",
    "InProcessBackend",
    "JobLifecycleBackend",
    "default_backend",
]
