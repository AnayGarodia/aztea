"""
package_finder.py — Find the best library for a task with live download stats

Input:
  {
    "task": "HTTP client with async support and retry logic",
    "ecosystem": "pypi|npm|both",   # default: pypi
    "count": 5                       # results per ecosystem (max 10)
  }

Output:
  {
    "task": str,
    "ecosystem": str,
    "results": [{
      "name": str,
      "description": str,
      "version": str,
      "weekly_downloads": int | null,
      "url": str,
      "score": float,
      "why": str
    }],
    "recommendation": str,
    "summary": str
  }
"""
from __future__ import annotations

import json
import re

import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_TIMEOUT = 10
_NPM_SEARCH = "https://registry.npmjs.org/-/v1/search"
_PYPI_PKG = "https://pypi.org/pypi/{name}/json"
_NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week/{name}"

_SUGGEST_SYSTEM = """\
You are a senior software engineer. Given a task description, list the best package names \
from the specified ecosystem. Return ONLY a JSON array of package name strings — exact names \
as they appear in the registry. No descriptions, no markdown, no explanation."""

_SUGGEST_USER = """\
Task: {task}
Ecosystem: {ecosystem}

List the {count} best package names for this task, best first.
Return JSON array only, e.g.: ["httpx", "aiohttp", "tenacity"]"""

_SYSTEM = """\
You are a senior software engineer recommending libraries. Given a task description and a list \
of candidate packages with their metadata, rank and explain why each fits (or doesn't fit) \
the task. Be concrete and honest about trade-offs.

Return ONLY valid JSON — no markdown fences, no prose outside the object."""

_USER = """\
Task: {task}
Ecosystem: {ecosystem}

Candidate packages:
{packages_json}

Return JSON:
{{
  "ranked": [
    {{
      "name": "package name",
      "why": "1-2 sentence explanation of why this fits (or caveat if it doesn't)",
      "score": 0.0-1.0
    }}
  ],
  "recommendation": "name of the single best package and why in one sentence",
  "summary": "2-3 sentence summary of the landscape for this task"
}}"""


def _llm_suggest_names(task: str, ecosystem: str, count: int) -> list[str]:
    """Ask the LLM to suggest package names, then validate via registry API."""
    try:
        resp = run_with_fallback(CompletionRequest(
            model="",
            messages=[
                Message("system", _SUGGEST_SYSTEM),
                Message("user", _SUGGEST_USER.format(task=task, ecosystem=ecosystem, count=count * 2)),
            ],
            max_tokens=200,
            json_mode=True,
        ))
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        names = json.loads(raw)
        if isinstance(names, list):
            return [str(n).strip() for n in names if str(n).strip()][:count * 2]
    except Exception:
        pass
    return []


def _fetch_pypi_search(query: str, count: int) -> list[dict]:
    """Get PyPI packages: LLM suggests names, registry API validates and enriches."""
    results = []
    names = _llm_suggest_names(query, "pypi", count)
    seen: set[str] = set()
    for name in names:
        if name in seen or len(results) >= count:
            break
        seen.add(name)
        try:
            pkg_resp = requests.get(
                _PYPI_PKG.format(name=name),
                timeout=_TIMEOUT,
                headers={"User-Agent": "aztea-package-finder/1.0"},
            )
            if pkg_resp.status_code != 200:
                continue
            info = pkg_resp.json().get("info", {})
            downloads = None
            try:
                dl_resp = requests.get(
                    f"https://pypistats.org/api/packages/{name}/recent",
                    timeout=_TIMEOUT,
                    headers={"User-Agent": "aztea-package-finder/1.0"},
                )
                if dl_resp.status_code == 200:
                    downloads = dl_resp.json().get("data", {}).get("last_week")
            except Exception:
                pass
            results.append({
                "name": name,
                "description": (info.get("summary") or "")[:200],
                "version": info.get("version", ""),
                "weekly_downloads": downloads,
                "url": f"https://pypi.org/project/{name}/",
            })
        except Exception:
            continue
    return results


def _fetch_npm_search(query: str, count: int) -> list[dict]:
    results = []
    try:
        resp = requests.get(
            _NPM_SEARCH,
            params={"text": query, "size": min(count, 20)},
            timeout=_TIMEOUT,
            headers={"User-Agent": "aztea-package-finder/1.0"},
        )
        if resp.status_code != 200:
            return results
        objects = resp.json().get("objects", [])
        for obj in objects[:count]:
            pkg = obj.get("package", {})
            name = pkg.get("name", "")
            if not name:
                continue
            # Fetch weekly downloads
            weekly = None
            try:
                dl_resp = requests.get(
                    _NPM_DOWNLOADS.format(name=name.replace("/", "%2F")),
                    timeout=_TIMEOUT,
                    headers={"User-Agent": "aztea-package-finder/1.0"},
                )
                if dl_resp.status_code == 200:
                    weekly = dl_resp.json().get("downloads")
            except Exception:
                pass
            results.append({
                "name": name,
                "description": (pkg.get("description") or "")[:200],
                "version": pkg.get("version", ""),
                "weekly_downloads": weekly,
                "url": f"https://www.npmjs.com/package/{name}",
                "score": obj.get("score", {}).get("final", 0.0),
            })
    except Exception:
        pass
    return results


def run(payload: dict) -> dict:
    task = str(payload.get("task") or "").strip()
    if not task:
        raise ValueError("'task' is required.")
    if len(task) > 500:
        task = task[:500]

    ecosystem = str(payload.get("ecosystem") or "pypi").strip().lower()
    if ecosystem not in ("pypi", "npm", "both"):
        ecosystem = "pypi"

    count = min(int(payload.get("count") or 5), 10)

    candidates: list[dict] = []

    if ecosystem in ("pypi", "both"):
        candidates.extend(_fetch_pypi_search(task, count))
    if ecosystem in ("npm", "both"):
        candidates.extend(_fetch_npm_search(task, count))

    if not candidates:
        return {
            "task": task,
            "ecosystem": ecosystem,
            "results": [],
            "recommendation": "",
            "summary": f"No packages found for '{task}' in {ecosystem}.",
        }

    # LLM ranking + explanation
    try:
        packages_json = json.dumps([
            {"name": c["name"], "description": c["description"], "weekly_downloads": c.get("weekly_downloads")}
            for c in candidates
        ], indent=2)
        resp = run_with_fallback(CompletionRequest(
            model="",
            messages=[
                Message("system", _SYSTEM),
                Message("user", _USER.format(task=task, ecosystem=ecosystem, packages_json=packages_json)),
            ],
            max_tokens=800,
            json_mode=True,
        ))
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        ranked = {r["name"]: r for r in parsed.get("ranked", [])}
        recommendation = parsed.get("recommendation", "")
        summary = parsed.get("summary", "")
    except Exception:
        ranked = {}
        recommendation = candidates[0]["name"] if candidates else ""
        summary = f"Found {len(candidates)} packages matching '{task}' in {ecosystem}."

    results = []
    for c in candidates:
        rank_info = ranked.get(c["name"], {})
        results.append({
            "name": c["name"],
            "description": c["description"],
            "version": c["version"],
            "weekly_downloads": c.get("weekly_downloads"),
            "url": c["url"],
            "score": rank_info.get("score", 0.5),
            "why": rank_info.get("why", ""),
        })

    # Sort by LLM score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "task": task,
        "ecosystem": ecosystem,
        "results": results,
        "recommendation": recommendation,
        "summary": summary,
    }
