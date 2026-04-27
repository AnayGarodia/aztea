"""Resolve pipeline input maps against prior step outputs."""

from __future__ import annotations

from typing import Any


def _lookup_path(source: Any, path: list[str], expr: str):
    current = source
    for segment in path:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        raise ValueError(f"Could not resolve '{expr}'. Missing segment '{segment}'.")
    return current


def _resolve_value(value: Any, pipeline_input: dict, step_results: dict[str, Any]):
    if isinstance(value, str):
        if value == "$input":
            return pipeline_input
        if value.startswith("$input."):
            return _lookup_path(pipeline_input, [part for part in value[len("$input."):].split(".") if part], value)
        if value.startswith("$"):
            parts = [part for part in value[1:].split(".") if part]
            if len(parts) >= 2 and parts[1] == "output":
                node_id = parts[0]
                if node_id not in step_results:
                    raise ValueError(f"Could not resolve '{value}'. Step '{node_id}' has not completed.")
                output = step_results[node_id]
                if len(parts) == 2:
                    return output
                return _lookup_path(output, parts[2:], value)
        return value
    if isinstance(value, dict):
        return {str(key): _resolve_value(item, pipeline_input, step_results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, pipeline_input, step_results) for item in value]
    return value


def resolve_input_map(input_map: dict, pipeline_input: dict, step_results: dict[str, Any]) -> dict:
    normalized_map = dict(input_map or {})
    return {
        str(key): _resolve_value(value, pipeline_input or {}, step_results or {})
        for key, value in normalized_map.items()
    }
