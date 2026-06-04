"""Value-add evidence: detect thin wrappers around a single library call.

# OWNS: a static AST heuristic that flags a Python handler whose whole body is
#   essentially ``import X; return X.f(payload)`` — a thin pass-through that adds
#   nothing over the caller running the library themselves.
# NOT OWNS: any blocking decision. This is a PROBATION SIGNAL ONLY (2026-06-03
#   decision D2). Verbatim OSS *code* is caught deterministically by the exact
#   fingerprint in ``core.listing_dedup``; there is no curated OSS-signature set
#   here (it rots and loses an arms race to trivial renames).
# INVARIANTS:
#   - Never raises on caller content; unparseable source → no signal.
#   - Emits WARN only. Boilerplate (module-level AgentServer scaffolding) is not
#     in the handler body, so it isn't counted (H5 false-positive guard).
"""
from __future__ import annotations

import ast
import logging
import sys

from core.listing_safety import LEVEL_WARN, VerificationFinding

_LOG = logging.getLogger(__name__)

# An agent handler may be `def` or `async def` — both are valid AgentServer entry
# points, so every helper accepts either.
_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef

CODE_THIN_WRAPPER = "listing.thin_wrapper"

# A handler with at most this many meaningful statements that just forwards to a
# third-party call is treated as a thin wrapper. Three lets through a guard +
# call + return; more than that is doing real work.
_THIN_WRAPPER_MAX_STATEMENTS = 3

_HANDLER_NAME_PREFERENCE = ("handler", "run", "main", "execute")

_STDLIB = set(getattr(sys, "stdlib_module_names", set()))


def assess_thin_wrapper(source: str) -> list[VerificationFinding]:
    """Pure: WARN when the handler is a trivial pass-through to one library call.

    Returns an empty list for substantive handlers, unparseable source, or when
    no handler function can be identified.
    """
    if not isinstance(source, str) or not source.strip():
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    fn = _pick_handler(tree)
    if fn is None:
        return []

    third_party = _third_party_imports(tree)
    body = _meaningful_body(fn)
    if len(body) > _THIN_WRAPPER_MAX_STATEMENTS:
        return []
    if not _returns_a_value(body):
        return []
    used = _external_call_targets(fn, third_party)
    if not used:
        return []

    return [
        VerificationFinding(
            code=CODE_THIN_WRAPPER,
            level=LEVEL_WARN,
            message=(
                "This agent looks like a thin wrapper around "
                f"{', '.join(sorted(used))} with little added logic. Wrappers "
                "that callers could run themselves start in probation; add real "
                "value (validation, synthesis, orchestration) to graduate faster."
            ),
            detail={"libraries": sorted(used), "statements": len(body)},
        )
    ]


def _pick_handler(tree: ast.Module) -> _FuncDef | None:
    """Pure: pick the entry function — preferred names first, else the sole public def."""
    funcs: list[_FuncDef] = [
        n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    by_name = {f.name: f for f in funcs}
    for name in _HANDLER_NAME_PREFERENCE:
        if name in by_name:
            return by_name[name]
    public = [f for f in funcs if not f.name.startswith("_")]
    if len(public) == 1:
        return public[0]
    return None


def _third_party_imports(tree: ast.Module) -> set[str]:
    """Pure: top-level module names that are not part of the standard library."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.asname or alias.name).split(".")[0]
                base = alias.name.split(".")[0]
                if base not in _STDLIB:
                    names.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module:
            base = node.module.split(".")[0]
            if base not in _STDLIB and node.level == 0:
                names.add(base)
    return names


def _meaningful_body(fn: _FuncDef) -> list[ast.stmt]:
    """Pure: handler body minus the docstring and bare ``pass``."""
    stmts = list(fn.body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    return [s for s in stmts if not isinstance(s, ast.Pass)]


def _returns_a_value(body: list[ast.stmt]) -> bool:
    return any(isinstance(s, ast.Return) and s.value is not None for s in body)


def _external_call_targets(fn: _FuncDef, third_party: set[str]) -> set[str]:
    """Pure: third-party module roots that the function actually calls into."""
    used: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        root = _call_root_name(node.func)
        if root and root in third_party:
            used.add(root)
    return used


def _call_root_name(func: ast.expr) -> str | None:
    """Pure: the leftmost Name of an attribute/call chain (``a.b.c()`` → ``a``)."""
    cur: ast.expr = func
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


__all__ = ["CODE_THIN_WRAPPER", "assess_thin_wrapper"]
