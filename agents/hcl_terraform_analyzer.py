"""
hcl_terraform_analyzer.py — Static security analysis of raw Terraform HCL.

Input:
  {
    "hcl_content": "<terraform.tf source>",   # required
    "frameworks": ["CIS", "PCI", "HIPAA"],     # optional compliance subset
    "skip_checks": ["CKV_AWS_20"]              # optional check skip list
  }

Output:
  {
    "tool": "checkov",
    "tool_version": str | null,
    "passed_count": int,
    "failed_count": int,
    "findings": [{
      "check_id": str,
      "check_name": str,
      "severity": "low|medium|high|critical",
      "resource": str,
      "file_line_range": [int, int],
      "guideline": str | null
    }],
    "summary": str
  }

OWNS: ingestion of raw HCL into a tempdir + invocation of ``checkov -d`` +
      shape of its JSON output.
NOT OWNS: plan-level analysis (terraform_plan_analyzer covers
          ``terraform plan -json``), CDK/Pulumi, cost estimation.
INVARIANTS:
  * checkov is invoked from the project venv; if absent, the agent
    returns ``tool_unavailable`` with refund.
  * Tempdir is removed even on exception.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from agents._contracts import agent_error as _err


_LOG = logging.getLogger(__name__)

_MAX_HCL_CHARS = 200_000
_CHECKOV_TIMEOUT_S = 60
_DEFAULT_SEVERITY_MAP = {
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
    "CRITICAL": "critical",
}


def _checkov_available() -> tuple[bool, str | None]:
    """Side-effect: detect whether ``checkov`` is on PATH; returns ``(available, version)``."""
    binary = shutil.which("checkov")
    if not binary:
        return False, None
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        version = (proc.stdout or proc.stderr or "").strip()
        return True, version or None
    except Exception:  # noqa: BLE001 — checkov missing or broken
        return False, None


def _normalize_severity(record: dict) -> str:
    """Pure: map checkov's severity field (or null) to our four-level enum."""
    raw = record.get("severity")
    if isinstance(raw, str):
        return _DEFAULT_SEVERITY_MAP.get(raw.upper(), "medium")
    return "medium"


def _shape_finding(record: dict) -> dict[str, Any]:
    """Pure: shape one checkov failed_check into the agent's record."""
    file_line_range = record.get("file_line_range") or [0, 0]
    if not isinstance(file_line_range, list) or len(file_line_range) < 2:
        file_line_range = [0, 0]
    return {
        "check_id": str(record.get("check_id") or ""),
        "check_name": str(record.get("check_name") or ""),
        "severity": _normalize_severity(record),
        "resource": str(record.get("resource") or ""),
        "file_line_range": [int(file_line_range[0]), int(file_line_range[1])],
        "guideline": record.get("guideline") or None,
    }


def _build_command(workdir: str, frameworks: list[str], skip_checks: list[str]) -> list[str]:
    """Pure: build the checkov CLI invocation list."""
    cmd = ["checkov", "-d", workdir, "--output", "json", "--quiet", "--soft-fail"]
    if frameworks:
        cmd.extend(["--framework", ",".join(frameworks)])
    if skip_checks:
        cmd.extend(["--skip-check", ",".join(skip_checks)])
    return cmd


def _parse_checkov_output(stdout: str) -> tuple[list[dict], int, int]:
    """Pure: parse checkov JSON into (failed, failed_count, passed_count).

    Why: checkov returns either a single dict for one framework or a list
    of dicts when multiple frameworks ran; normalise both shapes here.
    """
    if not stdout.strip():
        return [], 0, 0
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [], 0, 0
    documents = data if isinstance(data, list) else [data]
    findings: list[dict] = []
    failed = 0
    passed = 0
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        results = doc.get("results") or {}
        if not isinstance(results, dict):
            continue
        for check in results.get("failed_checks") or []:
            if isinstance(check, dict):
                findings.append(_shape_finding(check))
                failed += 1
        passed += len(results.get("passed_checks") or [])
    return findings, failed, passed


def _normalize_str_list(value: Any, field: str) -> list[str]:
    """Pure: validate-or-raise that ``value`` is a list of non-empty strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    out: list[str] = []
    for item in value:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out


def run(payload: dict) -> dict:
    """Run checkov over a single HCL string and return structured findings."""
    if not isinstance(payload, dict):
        return _err("hcl_terraform_analyzer.bad_input",
                    f"payload must be dict, got {type(payload).__name__}")
    hcl_content = str(payload.get("hcl_content") or "")
    if not hcl_content.strip():
        return _err(
            "hcl_terraform_analyzer.missing_hcl",
            "'hcl_content' is required (non-empty).",
        )
    if len(hcl_content) > _MAX_HCL_CHARS:
        return _err(
            "hcl_terraform_analyzer.hcl_too_large",
            f"hcl_content exceeds {_MAX_HCL_CHARS} chars",
        )
    try:
        frameworks = _normalize_str_list(payload.get("frameworks"), "frameworks")
        skip_checks = _normalize_str_list(payload.get("skip_checks"), "skip_checks")
    except ValueError as exc:
        return _err("hcl_terraform_analyzer.invalid_options", str(exc))
    available, version = _checkov_available()
    if not available:
        return _err(
            "hcl_terraform_analyzer.tool_unavailable",
            "checkov is not installed on this executor. The runtime image "
            "ships it via `pip install -r requirements.txt` (checkov>=3.2.0); "
            "if you're seeing this error in prod, the worker venv is stale — "
            "redeploy or run `pip install checkov>=3.2.0` on the worker. "
            "The call was not billed.",
        )
    tmpdir = tempfile.mkdtemp(prefix="aztea-hcl-")
    try:
        hcl_path = os.path.join(tmpdir, "main.tf")
        with open(hcl_path, "w", encoding="utf-8") as f:
            f.write(hcl_content)
        cmd = _build_command(tmpdir, frameworks, skip_checks)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_CHECKOV_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            return _err(
                "hcl_terraform_analyzer.timeout",
                f"checkov exceeded {_CHECKOV_TIMEOUT_S} s; use the async path "
                "for very large HCL bundles.",
            )
        findings, failed_count, passed_count = _parse_checkov_output(proc.stdout)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    severity_counts: dict[str, int] = {
        "critical": 0, "high": 0, "medium": 0, "low": 0,
    }
    for f in findings:
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
    summary = (
        f"Scanned with checkov: {failed_count} failed / {passed_count} passed. "
        f"By severity — critical {severity_counts['critical']}, "
        f"high {severity_counts['high']}, medium {severity_counts['medium']}, "
        f"low {severity_counts['low']}."
    )
    return {
        "tool": "checkov",
        "tool_version": version,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "findings": findings,
        "severity_counts": severity_counts,
        "summary": summary,
    }
