"""
openapi_validator.py — Validate OpenAPI 3.x specs and detect breaking changes.

Input:
  {
    "spec":       str,                   # required — OpenAPI YAML or JSON string
    "base_spec":  str | None,            # optional — previous version for diff
    "format":     "auto" | "yaml" | "json"  # default "auto"
  }

Output:
  {
    "valid": bool,
    "errors":   [{"path": str, "message": str, "severity": "error"}],
    "warnings": [{"path": str, "message": str}],
    "breaking_changes": [
      {"type": str, "path": str, "description": str, "severity": "breaking" | "info"}
    ],
    "stats": {
      "endpoints": int,
      "schemas":   int,
      "parameters": int,
      "openapi_version": str
    },
    "spec_title":   str,
    "spec_version": str,
    "tool_used":    "openapi-spec-validator" | "manual"
  }
"""

# OWNS: structural validation of OpenAPI 3.x specs, breaking-change detection
#       between two spec versions, heuristic warnings for common omissions.
# NOT OWNS: JSON Schema validation of request/response bodies against schema
#           definitions, live endpoint testing (see live_endpoint_tester.py),
#           authentication probing (see ai_red_teamer.py).
# INVARIANTS:
#   * run() must never raise — all failures return {"error": {"code", "message"}}.
#   * Only OpenAPI 3.x is supported; 2.x (Swagger) is rejected with a clear error.
#   * Errors capped at _MAX_ERRORS to avoid unbounded output on pathological specs.
# DECISIONS:
#   * openapi-spec-validator is used when available (full JSON-Schema-backed
#     validation); manual checks serve as a graceful fallback so the agent works
#     in minimal-dependency environments. The `tool_used` field tells callers which
#     path ran.
#   * Breaking-change severity is "breaking" for regressions (removed endpoints,
#     newly required params, removed response codes, changed schema types) and
#     "info" for additions (new endpoints). This mirrors what semver tooling
#     treats as a major vs minor bump.

from __future__ import annotations

import json
import logging
from typing import Any

from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_MAX_ERRORS = 20
_VALID_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_spec(raw: str, fmt: str) -> tuple[dict | None, str | None]:
    """Parse raw string into a dict.

    Returns (parsed_dict, error_message_or_None).
    fmt is "auto", "yaml", or "json".
    """
    raw = raw.strip()
    if fmt == "json" or (fmt == "auto" and raw.startswith("{")):
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            if fmt == "json":
                return None, f"JSON parse error: {exc}"
            # fall through to YAML for auto-detect
    try:
        import yaml  # type: ignore[import-untyped]
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            return None, "Spec parsed to a non-object type; expected a YAML/JSON object"
        return parsed, None
    except ImportError:
        # yaml not installed — try json one more time in case fmt was "auto"
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            return None, f"Could not parse spec (json failed, pyyaml not installed): {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"YAML parse error: {exc}"


# ---------------------------------------------------------------------------
# Validation with openapi-spec-validator
# ---------------------------------------------------------------------------


def _validate_with_library(parsed: dict) -> tuple[list[dict], bool]:
    """Use openapi-spec-validator when available; return (errors, library_available)."""
    try:
        from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator  # type: ignore[import-untyped]
    except ImportError:
        return [], False

    version_str = str(parsed.get("openapi", ""))
    validator_cls = OpenAPIV31SpecValidator if version_str.startswith("3.1") else OpenAPIV30SpecValidator
    errors: list[dict] = []
    try:
        for error in validator_cls(parsed).iter_errors():
            if len(errors) >= _MAX_ERRORS:
                break
            path = "/" + "/".join(
                str(p).replace("~", "~0").replace("/", "~1") for p in (error.absolute_path or [])
            )
            errors.append({"path": path or "/", "message": error.message, "severity": "error"})
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("openapi-spec-validator raised unexpectedly: %s", exc)
        errors.append({"path": "/", "message": f"Validator error: {exc}", "severity": "error"})

    return errors, True


# ---------------------------------------------------------------------------
# Manual structural checks (fallback when library not installed)
# ---------------------------------------------------------------------------


def _validate_manual(parsed: dict) -> list[dict]:
    """Lightweight structural checks when openapi-spec-validator is absent."""
    errors: list[dict] = []

    def _add(path: str, msg: str) -> None:
        if len(errors) < _MAX_ERRORS:
            errors.append({"path": path, "message": msg, "severity": "error"})

    if not parsed.get("openapi"):
        _add("/openapi", "Missing required field 'openapi'")

    info = parsed.get("info")
    if not isinstance(info, dict):
        _add("/info", "Missing required field 'info' (must be an object)")
    else:
        if not info.get("title"):
            _add("/info/title", "Missing required field 'info.title'")
        if not info.get("version"):
            _add("/info/version", "Missing required field 'info.version'")

    paths = parsed.get("paths")
    if paths is None:
        _add("/paths", "Missing required field 'paths'")
    elif not isinstance(paths, dict):
        _add("/paths", "'paths' must be an object")
    else:
        for path_key, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method in path_item:
                if method.startswith("x-") or method in ("parameters", "summary", "description"):
                    continue
                if method.lower() not in _VALID_HTTP_METHODS:
                    _add(f"/paths{path_key}/{method}", f"'{method}' is not a valid HTTP method")

    return errors


# ---------------------------------------------------------------------------
# Statistics extraction
# ---------------------------------------------------------------------------


def _extract_stats(parsed: dict) -> dict[str, Any]:
    """Count endpoints, schemas, and parameters from a parsed spec."""
    endpoints = parameters = 0
    for path_item in (parsed.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _VALID_HTTP_METHODS:
                continue
            endpoints += 1
            if isinstance(operation, dict) and isinstance(operation.get("parameters"), list):
                parameters += len(operation["parameters"])

    schema_map = ((parsed.get("components") or {}).get("schemas") or {})
    schemas = len(schema_map) if isinstance(schema_map, dict) else 0
    info = parsed.get("info") or {}
    return {
        "endpoints": endpoints,
        "schemas": schemas,
        "parameters": parameters,
        "openapi_version": str(parsed.get("openapi", "")),
        "spec_title": str(info.get("title", "")),
        "spec_version": str(info.get("version", "")),
    }


# ---------------------------------------------------------------------------
# Heuristic warnings
# ---------------------------------------------------------------------------


def _collect_warnings(parsed: dict) -> list[dict]:
    """Warn about common omissions that are not spec violations."""
    warnings: list[dict] = []
    if not (parsed.get("info") or {}).get("description"):
        warnings.append({"path": "/info/description", "message": "info.description is missing"})
    for path_key, path_item in (parsed.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _VALID_HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_path = f"/paths{path_key}/{method}"
            if not operation.get("summary"):
                warnings.append({"path": op_path, "message": "Operation is missing a 'summary'"})
            responses = operation.get("responses")
            if not responses or not isinstance(responses, dict) or not responses:
                warnings.append({"path": f"{op_path}/responses", "message": "Operation has no responses defined"})
    return warnings


# ---------------------------------------------------------------------------
# Breaking-change detection
# ---------------------------------------------------------------------------


def _collect_operations(parsed: dict) -> dict[str, Any]:
    """Return a flat map of 'METHOD /path' -> operation dict."""
    ops: dict[str, Any] = {}
    paths = parsed.get("paths") or {}
    for path_key, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _VALID_HTTP_METHODS:
                continue
            ops[f"{method.upper()} {path_key}"] = operation
    return ops


def _detect_breaking_changes(base: dict, new: dict) -> list[dict]:
    """Compare base spec to new spec and return a list of change descriptors."""
    changes: list[dict] = []
    base_ops = _collect_operations(base)
    new_ops = _collect_operations(new)

    for op_key in base_ops:
        if op_key not in new_ops:
            changes.append({"type": "endpoint_removed", "path": op_key,
                            "description": f"Endpoint removed: {op_key}", "severity": "breaking"})
    for op_key in new_ops:
        if op_key not in base_ops:
            changes.append({"type": "endpoint_added", "path": op_key,
                            "description": f"Endpoint added: {op_key}", "severity": "info"})

    base_paths = set((base.get("paths") or {}).keys())
    new_paths = set((new.get("paths") or {}).keys())
    for removed_path in base_paths - new_paths:
        changes.append({"type": "path_removed", "path": removed_path,
                        "description": f"Entire path removed: {removed_path}", "severity": "breaking"})

    for op_key in base_ops:
        if op_key not in new_ops:
            continue
        base_op = base_ops[op_key] or {}
        new_op = new_ops[op_key] or {}
        base_params = {p["name"]: p for p in (base_op.get("parameters") or [])
                       if isinstance(p, dict) and p.get("name")}
        new_params = {p["name"]: p for p in (new_op.get("parameters") or [])
                      if isinstance(p, dict) and p.get("name")}

        for name, param in new_params.items():
            if name not in base_params and param.get("required"):
                changes.append({"type": "required_param_added", "path": f"{op_key} > param:{name}",
                                 "description": f"New required parameter '{name}' added to {op_key}",
                                 "severity": "breaking"})
        for name in base_params:
            if name not in new_params and base_params[name].get("required"):
                changes.append({"type": "required_param_removed", "path": f"{op_key} > param:{name}",
                                 "description": f"Required parameter '{name}' removed from {op_key}",
                                 "severity": "info"})

        base_responses = base_op.get("responses") or {}
        new_responses = new_op.get("responses") or {}
        if isinstance(base_responses, dict) and isinstance(new_responses, dict):
            for code in base_responses:
                if code not in new_responses:
                    changes.append({"type": "response_code_removed", "path": f"{op_key} > response:{code}",
                                    "description": f"Response code {code} removed from {op_key}",
                                    "severity": "breaking"})

    base_schemas = ((base.get("components") or {}).get("schemas") or {})
    new_schemas = ((new.get("components") or {}).get("schemas") or {})
    if isinstance(base_schemas, dict) and isinstance(new_schemas, dict):
        for name in base_schemas:
            if name not in new_schemas:
                continue
            b_type = (base_schemas[name] or {}).get("type")
            n_type = (new_schemas[name] or {}).get("type")
            if b_type and n_type and b_type != n_type:
                changes.append({"type": "schema_type_changed",
                                 "path": f"/components/schemas/{name}",
                                 "description": f"Schema '{name}' type changed from '{b_type}' to '{n_type}'",
                                 "severity": "breaking"})
    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(payload: dict) -> dict:
    """Validate an OpenAPI 3.x spec and optionally detect breaking changes.

    Never raises. Returns a structured error envelope on bad input.
    """
    if not isinstance(payload, dict):
        return _err("openapi_validator.missing_spec", "payload must be an object")

    raw_spec = payload.get("spec")
    if not isinstance(raw_spec, str) or not raw_spec.strip():
        return _err(
            "openapi_validator.missing_spec",
            "'spec' is required and must be a non-empty string",
        )

    fmt = str(payload.get("format", "auto")).lower()
    if fmt not in {"auto", "yaml", "json"}:
        fmt = "auto"

    # --- Parse primary spec ---
    parsed, parse_err = _parse_spec(raw_spec, fmt)
    if parse_err or parsed is None:
        return _err("openapi_validator.parse_error", parse_err or "Failed to parse spec")

    # --- Version gate: only OpenAPI 3.x ---
    openapi_val = str(parsed.get("openapi", ""))
    if openapi_val and not openapi_val.startswith("3."):
        return _err(
            "openapi_validator.unsupported_version",
            f"Only OpenAPI 3.x is supported; spec declares '{openapi_val}'",
        )

    # --- Validate ---
    errors, library_available = _validate_with_library(parsed)
    tool_used = "openapi-spec-validator" if library_available else "manual"
    if not library_available:
        errors = _validate_manual(parsed)

    # --- Stats + warnings (always run on the parsed spec) ---
    stats_raw = _extract_stats(parsed)
    warnings = _collect_warnings(parsed)

    # --- Breaking-change detection (only when base_spec provided) ---
    breaking_changes: list[dict] = []
    raw_base = payload.get("base_spec")
    if isinstance(raw_base, str) and raw_base.strip():
        base_parsed, base_err = _parse_spec(raw_base, fmt)
        if base_err or base_parsed is None:
            warnings.append({
                "path": "/",
                "message": f"base_spec could not be parsed; skipping breaking-change detection: {base_err}",
            })
        else:
            try:
                breaking_changes = _detect_breaking_changes(base_parsed, parsed)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Breaking-change detection failed: %s", exc)
                warnings.append({
                    "path": "/",
                    "message": f"Breaking-change detection failed unexpectedly: {exc}",
                })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "breaking_changes": breaking_changes,
        "stats": {
            "endpoints": stats_raw["endpoints"],
            "schemas": stats_raw["schemas"],
            "parameters": stats_raw["parameters"],
            "openapi_version": stats_raw["openapi_version"],
        },
        "spec_title": stats_raw["spec_title"],
        "spec_version": stats_raw["spec_version"],
        "tool_used": tool_used,
    }
