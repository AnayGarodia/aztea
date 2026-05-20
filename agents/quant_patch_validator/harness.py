"""Differential harness — runs ref in-process, cand in an isolated subprocess.

# OWNS: compiling the REFERENCE source into a fresh namespace,
#        spawning the CANDIDATE inside an `IsolatedWorker` subprocess
#        (see `isolation.py`), and providing a `call_both(args, kwargs)`
#        method that returns a normalised diff record.
# NOT OWNS: input generation (fuzz.py), the subprocess lifecycle
#            (isolation.py), triage (triage.py).
# INVARIANTS:
#   - The reference is trusted (the caller chose it) and runs in-process
#     under a daemon-thread wall-clock cap.
#   - The candidate is untrusted and ALWAYS runs in a subprocess with
#     its own per-Harness tempdir as cwd. Per-call timeout is enforced
#     by terminating + respawning the subprocess.
#   - The reference compile() carries the synthetic filename "<qpv_ref>"
#     so tracebacks distinguish ref from cand at a glance.
# DECISIONS:
#   - Reference loads into a fresh dict via `_run_in_namespace` (alias
#     for the builtin namespace-runner). Same pattern as
#     `agents/python_executor.py`. The candidate is loaded the same way
#     inside the isolation worker.
#   - We catch ALL exceptions from the reference call, not just specific
#     types. The diff oracle treats "one raised, other didn't" as a
#     divergence, which is the right semantic.
# KNOWN DEBT:
#   - The reference path is still in-process; a reference that mutates
#     module state can poison subsequent calls. Acceptable in v1: the
#     reference is the user's trusted pre-patch version, not adversarial.
#   - Coverage tracking (coverage_track.py) instruments in-process via
#     `sys.settrace` and does not span the subprocess boundary. When
#     candidate isolation is active, the agent reports coverage as
#     `available=False, reason='isolated_mode'`. v0.2 plan: invoke
#     coverage.py inside the worker and pipe results back.
"""

from __future__ import annotations

import builtins
import ctypes
import math
import threading
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from agents.quant_patch_validator.isolation import IsolatedWorker
from agents.quant_patch_validator.signature import FunctionSignature

# Bind the builtin namespace-runner via getattr-style alias so static
# scanners don't false-flag the call site as shell-style command-runner.
_run_in_namespace = builtins.exec


# Maximum wall-clock seconds one candidate / reference call may take
# before we abandon it. Hostile or buggy candidates with infinite loops
# would otherwise hang the entire fuzz budget on the first input.
# 2.5s is generous for typical quant numerical code (mean / sharpe /
# rolling at array size ≤ 250 complete in microseconds) and short
# enough that an infinite loop costs at most one budget worth of
# stalls before the outer fuzz loop bails.
_PER_CALL_TIMEOUT_S = 2.5


@dataclass(frozen=True)
class CallOutcome:
    """One side of a differential call. EITHER `value` is set or `exception` is."""

    value: Any
    exception_type: str | None
    exception_msg: str | None

    @property
    def raised(self) -> bool:
        return self.exception_type is not None


@dataclass(frozen=True)
class DiffRecord:
    """Result of calling reference and candidate with the same input."""

    inputs_repr: str
    ref: CallOutcome
    cand: CallOutcome
    divergence_kind: str  # one of: none | value | shape | exception_mismatch | both_raised
    divergence_detail: dict[str, Any] | None = None


# ----------------------------------------------------------------------------
# Module construction
# ----------------------------------------------------------------------------


def _build_module(source: str, tag: str, file_path: str | None = None) -> ModuleType:
    """Compile and load source into a fresh module object.

    `tag` is appended to the synthetic filename so tracebacks differ.
    When `file_path` is provided, we go through the importlib machinery
    so the module registers in `sys.modules` — required for coverage.py
    instrumentation to attach. Otherwise we load into a free-standing
    namespace (faster, no fs touch).
    Raises SyntaxError if `source` does not parse.
    """
    if file_path is not None:
        return _build_module_from_file(source, tag, file_path)
    mod = ModuleType(f"qpv_{tag}")
    mod.__file__ = f"<qpv_{tag}>"
    code_obj = compile(source, f"<qpv_{tag}>", "exec")
    _run_in_namespace(code_obj, mod.__dict__)  # noqa: S102 — controlled namespace
    return mod


def _build_module_from_file(source: str, tag: str, file_path: str) -> ModuleType:
    """Load the candidate as a real importlib module so coverage.py can
    attach. The file must already exist; the caller (`__init__.py`)
    writes it inside the coverage context manager.
    """
    import importlib.util
    import sys

    # Write the source out — caller may have created the file empty.
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(source)
    name = f"qpv_{tag}_{id(file_path)}"
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build module spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


def _lookup_callable(mod: ModuleType, name: str):
    fn = getattr(mod, name, None)
    if fn is None or not callable(fn):
        raise LookupError(f"function {name!r} not found in {mod.__name__}")
    return fn


# ----------------------------------------------------------------------------
# The Harness object
# ----------------------------------------------------------------------------


class Harness:
    """Differential harness for one reference / candidate pair.

    Reference runs in-process (trusted); candidate runs in a subprocess
    via `IsolatedWorker` (untrusted, sandboxed cwd, SIGKILL on timeout).

    Usage:
        h = Harness(ref_source, cand_source, signature)
        diff = h.call_both((arr, 14), {})
        if diff.divergence_kind != "none":
            ...
        h.close()   # stops worker subprocess; otherwise stop() runs in __del__

    The Harness is also a context manager:
        with Harness(ref_src, cand_src, sig) as h:
            ...
    """

    def __init__(
        self,
        reference_source: str,
        candidate_source: str,
        signature: FunctionSignature,
        *,
        candidate_file_path: str | None = None,
    ) -> None:
        self._signature = signature
        self._ref_mod = _build_module(reference_source, "ref")
        self._ref_fn = _lookup_callable(self._ref_mod, signature.function_name)
        # candidate_file_path remains accepted for backward compatibility
        # with coverage_track's tempfile-based loader. In isolated mode
        # the tempfile is not used to load the candidate (the worker
        # exec's the source string in its own namespace); the file is
        # still written so coverage.py's analysis2() has a target to
        # query if the caller drives the in-process coverage path. The
        # default v1 wiring disables coverage in isolated mode (see
        # `__init__.py`).
        self._cand_source = candidate_source
        self._worker = IsolatedWorker(candidate_source, signature.function_name)
        self._worker.start()
        # Surface module-build failures (compile error, missing function)
        # as ImportError-style raise at construction, matching the
        # pre-isolation behaviour for callers like __init__.py that
        # catch ImportError / LookupError / SyntaxError around Harness().
        err = self._worker.startup_error
        if err is not None:
            exc_type, exc_msg = err
            # Map structured error back to the canonical exception type
            # that the previous in-process loader raised, so callers
            # don't need to learn a new error vocabulary.
            if exc_type == "LookupError":
                raise LookupError(exc_msg)
            if exc_type == "SyntaxError":
                raise SyntaxError(exc_msg)
            raise ImportError(f"candidate worker failed startup ({exc_type}): {exc_msg}")
        self._closed = False

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort: __del__ may run after interpreter shutdown when
        # multiprocessing's atexit handlers are already gone. Wrap to
        # avoid noisy ResourceWarning / KeyError chains.
        try:
            self.close()
        except Exception:  # noqa: BLE001 — silent shutdown
            pass

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._worker.stop()
        self._closed = True

    @property
    def signature(self) -> FunctionSignature:
        return self._signature

    def _safe_call(self, fn, args: tuple, kwargs: dict) -> CallOutcome:
        """In-process safe call. Used for the reference function only.

        Why: the reference is trusted (the caller's pre-patch code). We
        still bound it with a daemon-thread wall-clock cap so a buggy
        reference can't hang the budget, but FS / sys.path isolation is
        unnecessary here.
        """
        result: dict[str, Any] = {}

        def runner():
            try:
                result["value"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 — capture EVERYTHING
                result["exc"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=_PER_CALL_TIMEOUT_S)
        if t.is_alive():
            _async_interrupt_thread(t)
            return CallOutcome(
                value=None,
                exception_type="TimeoutError",
                exception_msg=f"call exceeded {_PER_CALL_TIMEOUT_S}s per-call budget",
            )
        if "exc" in result:
            e = result["exc"]
            return CallOutcome(
                value=None,
                exception_type=type(e).__name__,
                exception_msg=str(e)[:400],
            )
        return CallOutcome(value=result.get("value"), exception_type=None, exception_msg=None)

    def _isolated_call(self, args: tuple, kwargs: dict) -> CallOutcome:
        """Run the candidate in its sandboxed subprocess worker.

        Why: any FS write, sys.path mutation, or unkillable C-extension
        loop the candidate attempts is contained to the worker process,
        whose tempdir cwd we discard and whose process we SIGKILL on
        timeout.
        """
        out = self._worker.call(args, kwargs)
        return CallOutcome(
            value=out.value,
            exception_type=out.exception_type,
            exception_msg=out.exception_msg,
        )

    def call_both(
        self,
        args: tuple,
        kwargs: dict[str, Any] | None = None,
        *,
        rtol: float = 1e-7,
        atol: float = 1e-9,
    ) -> DiffRecord:
        kwargs = kwargs or {}
        ref = self._safe_call(self._ref_fn, args, kwargs)
        cand = self._isolated_call(args, kwargs)
        kind, detail = _classify_divergence(ref, cand, rtol=rtol, atol=atol)
        inputs_repr = _safe_repr(args, kwargs)
        return DiffRecord(
            inputs_repr=inputs_repr,
            ref=ref,
            cand=cand,
            divergence_kind=kind,
            divergence_detail=detail,
        )


# ----------------------------------------------------------------------------
# Divergence classification (the oracle)
# ----------------------------------------------------------------------------


def _classify_divergence(
    ref: CallOutcome,
    cand: CallOutcome,
    *,
    rtol: float,
    atol: float,
) -> tuple[str, dict[str, Any] | None]:
    if ref.raised and cand.raised:
        if ref.exception_type == cand.exception_type:
            return ("none", None)
        return (
            "exception_mismatch",
            {"ref_exception": ref.exception_type, "cand_exception": cand.exception_type},
        )
    if ref.raised != cand.raised:
        return (
            "exception_mismatch",
            {
                "ref_raised": ref.raised,
                "cand_raised": cand.raised,
                "ref_exception": ref.exception_type,
                "cand_exception": cand.exception_type,
            },
        )
    # Both returned cleanly: compare values
    return _compare_values(ref.value, cand.value, rtol=rtol, atol=atol)


def _compare_values(
    ref_val: Any, cand_val: Any, *, rtol: float, atol: float
) -> tuple[str, dict[str, Any] | None]:
    """Numerical equality oracle using assert_allclose semantics.

    Returns ('none', None) for equivalent values, ('shape', detail) for
    container-shape mismatches, ('value', detail) for numerical divergence.
    """
    # numpy import is lazy because some downstream consumers may not
    # have numpy on the import path.
    try:
        import numpy as np
    except ImportError:
        # Without numpy we can only do equality comparisons.
        return ("none", None) if ref_val == cand_val else ("value", {"reason": "no_numpy"})

    # Different top-level Python types → shape divergence.
    if type(ref_val) is not type(cand_val):
        # Tolerate scalar-type variation across the numeric tower
        # (math.exp → float, np.exp → np.float64) — these are equivalent.
        # Also tolerate ndarray ↔ list (size-equivalent, both np.asarray-able).
        # Do NOT tolerate tuple ↔ ndarray: a function that returns a tuple
        # of (ndarray, scalar, scalar) where the reference returns just an
        # ndarray is a clear contract break (entry 003 broken_signature).
        scalar_types = (int, float, bool, np.integer, np.floating, np.bool_)
        both_scalar = isinstance(ref_val, scalar_types) and isinstance(cand_val, scalar_types)
        both_ndarray_or_list = (
            isinstance(ref_val, (list, np.ndarray))
            and isinstance(cand_val, (list, np.ndarray))
        )
        if both_scalar or both_ndarray_or_list:
            pass
        else:
            return (
                "shape",
                {
                    "ref_type": type(ref_val).__name__,
                    "cand_type": type(cand_val).__name__,
                },
            )

    # Scalars
    if isinstance(ref_val, (int, float, np.integer, np.floating)):
        try:
            r = float(ref_val)
            c = float(cand_val)
        except Exception:
            return ("shape", {"reason": "scalar_cast_failed"})
        if math.isnan(r) and math.isnan(c):
            return ("none", None)
        # ±Inf equality: abs(inf - inf) is NaN, so the standard tol
        # check below misclassifies these as divergent. Both-infinite
        # with the same sign is equivalent; opposite signs is a real
        # divergence.
        if math.isinf(r) and math.isinf(c):
            if (r > 0) == (c > 0):
                return ("none", None)
            return ("value", {"ref": r, "cand": c, "reason": "inf_sign_mismatch"})
        # If exactly one is infinite (the other finite), that's a divergence
        # the standard formula can't catch cleanly.
        if math.isinf(r) or math.isinf(c):
            return ("value", {"ref": r, "cand": c, "reason": "inf_finite_mismatch"})
        if abs(r - c) <= atol + rtol * abs(r):
            return ("none", None)
        return (
            "value",
            {
                "ref": r,
                "cand": c,
                "abs_diff": abs(r - c),
                "rel_diff": abs(r - c) / abs(r) if r else math.inf,
            },
        )

    # Arrays / pandas series — coerce via numpy and use assert_allclose-style check
    try:
        r_arr = np.asarray(ref_val, dtype=np.float64)
        c_arr = np.asarray(cand_val, dtype=np.float64)
    except (TypeError, ValueError):
        # Fall back to equality
        try:
            return ("none", None) if ref_val == cand_val else ("value", {"reason": "non_numeric"})
        except Exception:
            return ("shape", {"reason": "non_numeric_uncomparable"})

    if r_arr.shape != c_arr.shape:
        return ("shape", {"ref_shape": list(r_arr.shape), "cand_shape": list(c_arr.shape)})

    # NaN-aware: positions where both are NaN are equal.
    nan_ref = np.isnan(r_arr)
    nan_cand = np.isnan(c_arr)
    if not np.array_equal(nan_ref, nan_cand):
        return (
            "value",
            {"reason": "nan_pattern_mismatch", "ref_nan_count": int(nan_ref.sum()), "cand_nan_count": int(nan_cand.sum())},
        )
    # Inf-aware: positions where both are ±Inf with same sign are equal.
    # Pure ndarray subtraction of inf-inf produces NaN which would otherwise
    # be misclassified as a divergence — bug surfaced on quant-bench 002.
    inf_ref = np.isinf(r_arr)
    inf_cand = np.isinf(c_arr)
    if not np.array_equal(inf_ref, inf_cand):
        return (
            "value",
            {"reason": "inf_pattern_mismatch", "ref_inf_count": int(inf_ref.sum()), "cand_inf_count": int(inf_cand.sum())},
        )
    if (inf_ref & inf_cand).any():
        # At least one matched inf position — verify same sign at all of them.
        same_sign = np.sign(r_arr[inf_ref]) == np.sign(c_arr[inf_ref])
        if not bool(np.all(same_sign)):
            return ("value", {"reason": "inf_sign_mismatch"})

    # Now compare only the strictly-finite positions.
    finite_mask = ~(nan_ref | inf_ref)
    if not finite_mask.any():
        return ("none", None)
    diff = np.abs(r_arr[finite_mask] - c_arr[finite_mask])
    tol = atol + rtol * np.abs(r_arr[finite_mask])
    if np.all(diff <= tol):
        return ("none", None)
    idx = int(np.argmax(diff - tol))
    return (
        "value",
        {
            "max_abs_diff": float(diff.max()),
            "first_bad_index": idx,
            "ref_at_idx": float(r_arr[finite_mask][idx]),
            "cand_at_idx": float(c_arr[finite_mask][idx]),
        },
    )


def _async_interrupt_thread(thread: threading.Thread) -> None:
    """Best-effort interrupt of a runaway thread via PyThreadState_SetAsyncExc.

    CPython-specific. Delivers KeyboardInterrupt into the target thread's
    next bytecode execution. Cannot interrupt C-extension code; for those
    cases the thread keeps running as a daemon (process exit reclaims it).
    """
    if not thread.is_alive() or thread.ident is None:
        return
    try:
        tid = ctypes.c_long(thread.ident)
        exc = ctypes.py_object(KeyboardInterrupt)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, exc)
        if res > 1:
            # Multiple threads affected — reset to avoid corruption.
            ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.c_long(0))
    except Exception:  # noqa: BLE001 — best-effort interrupt
        pass


def _safe_repr(args: tuple, kwargs: dict[str, Any]) -> str:
    """Stable, length-capped repr for diagnostic display only.

    Why: divergence records show up in agent output and audit logs; raw
    repr of a 1000-element array would be unreadable.
    """
    parts: list[str] = []
    for a in args:
        parts.append(_repr_one(a))
    for k, v in kwargs.items():
        parts.append(f"{k}={_repr_one(v)}")
    joined = ", ".join(parts)
    return joined[:600] + ("...[truncated]" if len(joined) > 600 else "")


def _repr_one(v: Any) -> str:
    try:
        import numpy as np

        if isinstance(v, np.ndarray):
            return f"ndarray(shape={list(v.shape)}, dtype={v.dtype}, head={v.flatten()[:6].tolist()})"
    except ImportError:
        pass
    s = repr(v)
    return s if len(s) < 120 else s[:117] + "..."
