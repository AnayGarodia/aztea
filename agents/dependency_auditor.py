"""
dependency_auditor.py — Dependency audit agent for package.json / requirements.txt

Input:
  {
    "manifest": "contents of package.json or requirements.txt",
    "ecosystem": "npm|pypi|auto",
    "checks": ["cve", "outdated", "license"]   # default: all three
  }

Output:
  {
    "ecosystem": str,
    "total_packages": int,
    "vulnerable_count": int,
    "outdated_count": int,
    "critical_count": int,
    "packages": [{
      "name": str,
      "current_version": str,
      "latest_version": str | null,
      "cves": [{"id": str, "severity": str, "description": str, "fixed_in": str | null}],
      "license": str | null,
      "license_risk": "none|low|medium|high",
      "action": "upgrade|replace|review|ok"
    }],
    "top_priorities": [str],
    "summary": str
  }
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a security engineer specializing in supply chain and dependency risk. You audit package \
manifests for vulnerabilities, outdated packages, and license issues.

For each package:
1. Identify known CVEs from your training data (note the knowledge cutoff clearly when relevant)
2. Assess whether the current version is likely outdated based on common versioning patterns
3. Identify license type and flag copyleft licenses (GPL, AGPL) that may be incompatible with commercial use
4. Assign a concrete action: upgrade (known CVE fix), replace (abandoned/vulnerable with no fix), \
   review (license concern), ok (no issues found)

Be honest about uncertainty — if you don't know the latest version, say so rather than guessing.
Flag only real, known issues, not hypothetical risks.

Return ONLY valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Audit this {ecosystem} dependency manifest.
Checks to perform: {checks}

Manifest:
{manifest}

Return a JSON object:
{{
  "ecosystem": "npm|pypi",
  "total_packages": integer,
  "vulnerable_count": integer (packages with known CVEs),
  "outdated_count": integer (packages likely needing upgrade),
  "critical_count": integer (critical or high severity CVEs),
  "packages": [
    {{
      "name": "package name",
      "current_version": "version from manifest",
      "latest_version": "known latest or null if unknown",
      "cves": [
        {{
          "id": "CVE-YYYY-XXXXX",
          "severity": "critical|high|medium|low",
          "description": "brief exploit description",
          "fixed_in": "version that fixes it or null"
        }}
      ],
      "license": "SPDX license identifier or null",
      "license_risk": "none|low|medium|high",
      "action": "upgrade|replace|review|ok"
    }}
  ],
  "top_priorities": ["ordered list of most urgent actions"],
  "summary": "2–3 sentence risk summary with overall posture"
}}"""

_MAX_MANIFEST_CHARS = 10_000


def _detect_ecosystem(manifest: str) -> str:
    stripped = manifest.strip()
    if stripped.startswith("{"):
        return "npm"
    return "pypi"


def run(payload: dict) -> dict:
    manifest = str(payload.get("manifest") or "").strip()
    if not manifest:
        raise ValueError("'manifest' is required (contents of package.json or requirements.txt).")

    ecosystem = str(payload.get("ecosystem") or "auto")
    if ecosystem == "auto":
        ecosystem = _detect_ecosystem(manifest)

    checks = payload.get("checks")
    if not checks or not isinstance(checks, list):
        checks = ["cve", "outdated", "license"]
    checks_str = ", ".join(str(c) for c in checks)

    prompt = _USER.format(
        ecosystem=ecosystem,
        checks=checks_str,
        manifest=manifest[:_MAX_MANIFEST_CHARS],
    )

    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", prompt)],
        max_tokens=3000,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON: {e}\n\n{raw[:300]}") from e


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
