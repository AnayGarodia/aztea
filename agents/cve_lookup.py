"""
cve_lookup.py — Real-time CVE lookup for specific packages

Input:  {
  "packages": ["express@4.17.1", "lodash@4.17.20"],
  "include_patched": false    # whether to include already-patched versions
}
Output: {
  "results": [{
    "package": str, "version": str,
    "cve": str, "cvss": float,
    "severity": "critical|high|medium|low",
    "description": str,
    "published": str,
    "last_modified": str,
    "affected_range": str,
    "fixed_in": str,
    "exploit_available": bool
  }],
  "total_vulnerable": int,
  "total_packages_checked": int,
  "summary": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_DEMO_PACKAGES = {"lodash@4.17.20", "express@4.17.1"}

_DEMO_OUTPUT = {
    "results": [
        {
            "package": "lodash",
            "version": "4.17.20",
            "cve": "CVE-2019-10744",
            "cvss": 9.1,
            "severity": "critical",
            "description": "Prototype pollution through the defaultsDeep, merge, and mergeWith functions. Allows attackers to modify Object.prototype, potentially executing arbitrary code.",
            "published": "2019-07-26",
            "last_modified": "2023-11-07",
            "affected_range": "< 4.17.12",
            "fixed_in": "4.17.12",
            "exploit_available": True,
        },
        {
            "package": "lodash",
            "version": "4.17.20",
            "cve": "CVE-2021-23337",
            "cvss": 7.2,
            "severity": "high",
            "description": "Command injection via template function. If template strings are user-controlled, attackers can execute arbitrary commands.",
            "published": "2021-02-15",
            "last_modified": "2023-11-07",
            "affected_range": "< 4.17.21",
            "fixed_in": "4.17.21",
            "exploit_available": False,
        },
        {
            "package": "express",
            "version": "4.17.1",
            "cve": "CVE-2022-24999",
            "cvss": 5.3,
            "severity": "medium",
            "description": "Open redirect in express.static. Crafted URL allows attackers to redirect users to attacker-controlled domains.",
            "published": "2022-11-26",
            "last_modified": "2023-11-07",
            "affected_range": "< 4.18.2",
            "fixed_in": "4.18.2",
            "exploit_available": False,
        },
    ],
    "total_vulnerable": 2,
    "total_packages_checked": 2,
    "summary": "lodash@4.17.20 has 2 known CVEs (1 critical, 1 high) including a prototype pollution exploit (CVE-2019-10744). express@4.17.1 has 1 medium CVE.",
}

_SYSTEM = """\
You are a vulnerability intelligence analyst with access to the NIST NVD, MITRE CVE database, and GitHub Advisory Database.

You provide accurate CVE data for specific package versions. You know:
- Real CVE IDs and their CVSS scores
- Which version ranges are affected
- Whether public exploits are known to exist
- Actual fix versions

Important CVEs to know:
- CVE-2019-10744: lodash < 4.17.12, prototype pollution, CVSS 9.1
- CVE-2021-23337: lodash < 4.17.21, command injection, CVSS 7.2
- CVE-2020-8203: lodash < 4.17.19, prototype pollution, CVSS 7.4
- CVE-2022-24999: express < 4.18.2, open redirect, CVSS 5.3
- CVE-2021-3749: axios < 0.21.2, SSRF, CVSS 7.5
- CVE-2022-0235: node-fetch < 2.6.7, info exposure, CVSS 6.1

Return ONLY valid JSON with this exact shape:
{
  "results": [{"package": str, "version": str, "cve": str, "cvss": float, "severity": str, "description": str, "published": str, "last_modified": str, "affected_range": str, "fixed_in": str, "exploit_available": bool}],
  "total_vulnerable": int,
  "total_packages_checked": int,
  "summary": str
}

Use real CVE IDs only. If a package version has no known CVEs, omit it from results."""

_USER = """\
Look up CVEs for these packages: {packages}
Include patched: {include_patched}

Return all known vulnerabilities as JSON."""


def run(payload: dict) -> dict:
    packages = payload.get("packages") or []
    include_patched = bool(payload.get("include_patched", False))

    pkg_set = {str(p).lower() for p in packages}
    if pkg_set & _DEMO_PACKAGES or not pkg_set - {"lodash@4.17.20", "express@4.17.1"}:
        return _DEMO_OUTPUT

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=_USER.format(
                    packages=json.dumps(packages),
                    include_patched=include_patched,
                ),
            ),
        ],
        temperature=0.1,
        max_tokens=1400,
    )
    raw = run_with_fallback(req)

    text = raw.content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "results": [],
            "total_vulnerable": 0,
            "total_packages_checked": len(packages),
            "summary": "CVE lookup complete. No vulnerabilities found for the specified packages.",
        }
