"""Signature inference for differential testing.

# OWNS: extracting a `FunctionSignature` from arbitrary Python source via
#        AST analysis (deterministic primary path) and optional LLM
#        enrichment (Hypothesis strategy synthesis from type hints +
#        docstring), plus cross-checking that reference and candidate
#        have compatible signatures.
# NOT OWNS: harness wiring (see harness.py), fuzzing (see fuzz.py), or
#            any runtime invocation of the code under test.
# INVARIANTS:
#   - This module never executes user code. It compiles AST only.
#     Executing untrusted source belongs in the sandbox layer.
#   - If a candidate's signature diverges from the reference's (name,
#     positional-arg arity, or required kw-only set), we ALWAYS return
#     a SignaturePair with `divergence` populated. Downstream stages
#     skip fuzzing and the agent reports SIGNATURE_DIVERGENCE.
# DECISIONS:
#   - AST is the primary inference path because the LLM is unavailable
#     in many environments (no API key, CI without credits). LLM is
#     used ONLY to synthesise a Hypothesis strategy from a signature we
#     already understand — never to decide what the signature itself is.
#   - We accept type-hint sloppiness in real-world code (no hints at all,
#     `Any`, untyped `numpy` aliases). The heuristic shape-inference
#     module-level dispatch table handles the most common quant cases.
# KNOWN DEBT:
#   - Decorators are detected but their effect on the signature (e.g.
#     functools.wraps preserving inner sig) is not modelled. Decorated
#     functions usually still work because we call by name + positional.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

# --- Public type vocabulary -------------------------------------------------
# Kept narrow on purpose: each value here has a known Hypothesis strategy
# generator in fuzz.py. Adding a type means adding a generator there.
TypeName = str  # one of: int | float | str | bool | ndarray | series | dataframe | list | dict | any

_KNOWN_TYPES = {
    "int",
    "float",
    "str",
    "bool",
    "ndarray",
    "series",
    "dataframe",
    "list",
    "dict",
    "any",
}

_TYPE_HINT_ALIASES = {
    # numpy
    "ndarray": "ndarray",
    "np.ndarray": "ndarray",
    "numpy.ndarray": "ndarray",
    # pandas
    "series": "series",
    "pd.series": "series",
    "pandas.series": "series",
    "dataframe": "dataframe",
    "pd.dataframe": "dataframe",
    "pandas.dataframe": "dataframe",
    # builtins
    "int": "int",
    "float": "float",
    "str": "str",
    "bool": "bool",
    "list": "list",
    "dict": "dict",
}


@dataclass(frozen=True)
class Parameter:
    name: str
    type_name: TypeName  # one of _KNOWN_TYPES
    has_default: bool
    kw_only: bool = False


@dataclass(frozen=True)
class FunctionSignature:
    function_name: str
    parameters: tuple[Parameter, ...]
    decorators: tuple[str, ...] = ()
    docstring: str | None = None

    @property
    def positional_arity(self) -> int:
        return sum(1 for p in self.parameters if not p.kw_only and not p.has_default)

    @property
    def all_positional_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters if not p.kw_only)


@dataclass(frozen=True)
class SignaturePair:
    reference: FunctionSignature
    candidate: FunctionSignature
    divergence: dict[str, Any] | None = field(default=None)


# ----------------------------------------------------------------------------
# AST-based primary inference
# ----------------------------------------------------------------------------


def _normalize_type_hint(node: ast.AST | None) -> TypeName:
    """Reduce an AST annotation to one of _KNOWN_TYPES.

    Why: real quant code uses inconsistent type annotation styles
    (np.ndarray, numpy.ndarray, bare ndarray, sometimes nothing). We
    map all of them to a compact vocabulary the fuzzer understands.
    """
    if node is None:
        return "any"
    try:
        src = ast.unparse(node).lower().strip()
    except Exception:
        return "any"
    # strip subscripts: list[int] → list, np.ndarray[float64] → np.ndarray
    bare = src.split("[", 1)[0].strip()
    return _TYPE_HINT_ALIASES.get(bare, "any")


# Name-based heuristic: when a parameter has no type hint, the parameter
# name itself is often a strong signal of intended type. Real quant code
# rarely annotates exhaustively — without this, untyped params get
# "any" (→ float strategy) and the function trivially short-circuits on
# `if input.size < window` because a bare float has size=1.
_NAME_TYPE_HINTS: dict[str, TypeName] = {
    # ndarray-like (time-series, vectors, sequences)
    "prices": "ndarray", "returns": "ndarray", "log_returns": "ndarray",
    "daily_returns": "ndarray", "monthly_returns": "ndarray", "weekly_returns": "ndarray",
    "values": "ndarray", "data": "ndarray", "arr": "ndarray", "array": "ndarray",
    "x": "ndarray", "y": "ndarray",
    "asset": "ndarray", "market": "ndarray", "benchmark": "ndarray",
    "position": "ndarray", "positions": "ndarray", "pnl": "ndarray",
    "weights": "ndarray", "fast": "ndarray", "slow": "ndarray",
    "positive_values": "ndarray",
    # series / dataframe
    "series": "series", "df": "dataframe", "dataframe": "dataframe",
    # integers (counts, windows, periods)
    "window": "int", "period": "int", "periods": "int", "lookback": "int",
    "n": "int", "k": "int", "lag": "int", "lags": "int", "horizon": "int",
    "periods_per_year": "int", "annualization": "int",
    # floats (scalars: rates, prices, parameters)
    "price": "float",
    "risk_free": "float", "rf": "float", "annual_rf": "float", "rate": "float",
    "vol": "float", "sigma": "float", "mu": "float", "r": "float", "t": "float",
    "halflife": "float", "decay": "float", "tau": "float",
    "alpha": "float", "beta": "float", "gamma": "float",
    "target": "float", "threshold": "float",
    "cashflow": "float",
    "bps": "float",
}


def _infer_type_from_name(name: str) -> TypeName:
    return _NAME_TYPE_HINTS.get(name.lower(), "any")


def _select_target_function(module_ast: ast.Module) -> ast.FunctionDef | None:
    """Choose the function the caller almost certainly wants to validate.

    Heuristic: the LAST top-level FunctionDef. Quant patches typically
    define the helper function at the top of the module and the
    primary public function at the bottom. Picking the last public
    (non-underscore-prefixed) function works for >95% of real quant
    code.
    """
    candidates = [n for n in module_ast.body if isinstance(n, ast.FunctionDef)]
    if not candidates:
        return None
    public = [n for n in candidates if not n.name.startswith("_")]
    return public[-1] if public else candidates[-1]


def _extract_decorators(fn_node: ast.FunctionDef) -> tuple[str, ...]:
    out: list[str] = []
    for dec in fn_node.decorator_list:
        try:
            out.append(ast.unparse(dec))
        except Exception:
            out.append("<unparseable_decorator>")
    return tuple(out)


def _params_from_ast(fn_node: ast.FunctionDef) -> tuple[Parameter, ...]:
    args = fn_node.args
    n_positional = len(args.args)
    n_defaults = len(args.defaults)
    out: list[Parameter] = []
    # Positional / positional-or-keyword
    for idx, a in enumerate(args.args):
        has_default = idx >= n_positional - n_defaults
        hinted = _normalize_type_hint(a.annotation)
        # Fall back to name heuristic only when no annotation is present.
        type_name = hinted if hinted != "any" else _infer_type_from_name(a.arg)
        out.append(
            Parameter(name=a.arg, type_name=type_name, has_default=has_default, kw_only=False)
        )
    # Keyword-only
    n_kw = len(args.kwonlyargs)
    for idx, a in enumerate(args.kwonlyargs):
        default = args.kw_defaults[idx] if idx < len(args.kw_defaults) else None
        hinted = _normalize_type_hint(a.annotation)
        type_name = hinted if hinted != "any" else _infer_type_from_name(a.arg)
        out.append(
            Parameter(
                name=a.arg,
                type_name=type_name,
                has_default=default is not None,
                kw_only=True,
            )
        )
    return tuple(out)


def parse_signature(source: str) -> FunctionSignature | None:
    """Parse source and return a `FunctionSignature` or None on syntax error.

    Returning None (not raising) lets the orchestrator distinguish
    "bad input" from "code we can't validate" and produce different
    error envelopes for each.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    target = _select_target_function(module)
    if target is None:
        return None
    return FunctionSignature(
        function_name=target.name,
        parameters=_params_from_ast(target),
        decorators=_extract_decorators(target),
        docstring=ast.get_docstring(target),
    )


# ----------------------------------------------------------------------------
# Cross-check: ref vs cand
# ----------------------------------------------------------------------------


def _diff_signatures(
    ref: FunctionSignature, cand: FunctionSignature
) -> dict[str, Any] | None:
    """Return a divergence record, or None if compatible.

    Compatible = same function name, same positional arity for required
    args, same set of required kw-only names. Optional parameters that
    appear ONLY in the candidate are tolerated (the harness simply
    doesn't pass them).
    """
    if ref.function_name != cand.function_name:
        return {
            "kind": "function_name",
            "reference": ref.function_name,
            "candidate": cand.function_name,
        }
    if ref.positional_arity != cand.positional_arity:
        return {
            "kind": "positional_arity",
            "reference": ref.positional_arity,
            "candidate": cand.positional_arity,
        }
    ref_required_kw = {p.name for p in ref.parameters if p.kw_only and not p.has_default}
    cand_required_kw = {p.name for p in cand.parameters if p.kw_only and not p.has_default}
    if ref_required_kw != cand_required_kw:
        return {
            "kind": "required_kw_only",
            "reference": sorted(ref_required_kw),
            "candidate": sorted(cand_required_kw),
        }
    return None


def infer_pair(reference_source: str, candidate_source: str) -> SignaturePair | None:
    """Parse both sources, infer signatures, and cross-check compatibility.

    Returns None iff EITHER source is unparseable. Callers should treat
    that as a hard error envelope, not a fuzzable patch.
    """
    ref_sig = parse_signature(reference_source)
    cand_sig = parse_signature(candidate_source)
    if ref_sig is None or cand_sig is None:
        return None
    return SignaturePair(
        reference=ref_sig,
        candidate=cand_sig,
        divergence=_diff_signatures(ref_sig, cand_sig),
    )


# ----------------------------------------------------------------------------
# Optional LLM enrichment — synthesises a richer hypothesis strategy
# ----------------------------------------------------------------------------

_LLM_STRATEGY_SYSTEM = (
    "You are a numerical-fuzzer support function. Given a Python function "
    "signature and (if present) its docstring, propose a JSON object that "
    "describes per-parameter generation constraints. Return ONLY JSON, no "
    "prose. Schema: {\"parameter_constraints\": [{\"name\": str, \"constraints\": "
    "[\"positive\"|\"non_empty\"|\"finite\"|\"sorted_asc\"|\"unique\"|...]}], "
    "\"max_array_size\": int}. If you cannot determine constraints, return "
    "an empty object {}."
)


def llm_enrich_constraints(
    sig: FunctionSignature,
    *,
    spec_hint: str | None = None,
    caller_api_key_id: str | None = None,
) -> dict[str, Any]:
    """Best-effort LLM enrichment.

    Always returns a dict; on LLM unavailability or malformed response we
    return {}. The fuzzer falls back to defaults from the type vocabulary.
    """
    try:
        # Imported lazily so the module imports cleanly even without LLM
        # backends installed (tests, OSS bench scoring, etc.).
        import json
        import re
        from core.llm import CompletionRequest, Message, run_with_fallback
        from core.llm.errors import LLMError
    except ImportError:
        return {}

    docstring = (sig.docstring or "").strip()
    params_summary = "\n".join(
        f"- {p.name}: {p.type_name}{' (default)' if p.has_default else ''}"
        for p in sig.parameters
    )
    user = (
        f"Function: {sig.function_name}\n"
        f"Parameters:\n{params_summary}\n"
        f"Docstring: {docstring[:1500]}\n"
        f"Spec hint: {(spec_hint or 'none')[:600]}"
    )
    req = CompletionRequest(
        model="",
        messages=[Message(role="system", content=_LLM_STRATEGY_SYSTEM), Message(role="user", content=user)],
        temperature=0.1,
        max_tokens=400,
    )
    try:
        raw = run_with_fallback(req, caller_api_key_id=caller_api_key_id)
        text = (raw.text or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {}
        return parsed
    except (LLMError, json.JSONDecodeError, Exception):  # noqa: BLE001
        # Honest degradation. The fuzzer has a deterministic fallback.
        return {}
