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
      shape of its JSON output; limited static fallback when checkov is absent.
NOT OWNS: plan-level analysis (terraform_plan_analyzer covers
          ``terraform plan -json``), CDK/Pulumi, cost estimation.
INVARIANTS:
  * checkov is invoked from the project venv when present.
  * If checkov is absent, the agent returns a clearly labelled limited
    fallback result rather than a raw infrastructure failure.
  * Tempdir is removed even on exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from agents._contracts import agent_error as _err


_LOG = logging.getLogger(__name__)

_MAX_HCL_CHARS = 200_000
_CHECKOV_TIMEOUT_S = 60
_STATIC_RULES = (
    {
        "pattern": re.compile(r"(?is)resource\s+\"aws_security_group_rule\".*?cidr_blocks\s*=\s*\[\s*\"0\.0\.0\.0/0\"\s*\].*?from_port\s*=\s*22"),
        "check_id": "AZTEA_HCL_001",
        "check_name": "SSH exposed to the public internet",
        "severity": "high",
    },
    {
        "pattern": re.compile(r"(?is)resource\s+\"aws_s3_bucket_public_access_block\".*?block_public_acls\s*=\s*false"),
        "check_id": "AZTEA_HCL_002",
        "check_name": "S3 public ACL block disabled",
        "severity": "high",
    },
    {
        "pattern": re.compile(r"(?is)resource\s+\"aws_db_instance\".*?publicly_accessible\s*=\s*true"),
        "check_id": "AZTEA_HCL_003",
        "check_name": "Database instance publicly accessible",
        "severity": "critical",
    },
    {
        "pattern": re.compile(r"(?is)resource\s+\"aws_s3_bucket\".*?acl\s*=\s*\"public-read\""),
        "check_id": "AZTEA_HCL_004",
        "check_name": "S3 bucket uses public-read ACL",
        "severity": "high",
    },
)
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


def _static_fallback_scan(hcl_content: str) -> dict[str, Any]:
    """Run a tiny local rule set when checkov is absent so callers get signal."""
    findings: list[dict[str, Any]] = []
    for rule in _STATIC_RULES:
        for match in rule["pattern"].finditer(hcl_content):
            start_line = hcl_content.count("\n", 0, match.start()) + 1
            end_line = hcl_content.count("\n", 0, match.end()) + 1
            findings.append(
                {
                    "check_id": rule["check_id"],
                    "check_name": rule["check_name"],
                    "severity": rule["severity"],
                    "resource": "unknown",
                    "file_line_range": [start_line, end_line],
                    "guideline": None,
                }
            )
    severity_counts: dict[str, int] = {
        "critical": 0, "high": 0, "medium": 0, "low": 0,
    }
    for finding in findings:
        severity = str(finding["severity"])
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    return {
        "tool": "aztea-static-hcl-fallback",
        "tool_version": None,
        "passed_count": 0,
        "failed_count": len(findings),
        "findings": findings,
        "severity_counts": severity_counts,
        "summary": (
            "checkov is not installed on this executor, so Aztea ran a "
            f"limited built-in HCL rule set: {len(findings)} finding(s)."
        ),
        "warning": "Install checkov on the worker for the full policy corpus.",
    }


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
        return _static_fallback_scan(hcl_content)
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
