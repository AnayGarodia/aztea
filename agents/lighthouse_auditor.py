"""
lighthouse_auditor.py — Run Google Lighthouse against any public URL.

Input:
  {
    "url": "https://example.com",                # required
    "categories": ["performance", "accessibility",
                   "best-practices", "seo", "pwa"],  # optional, default first 4
    "strategy": "mobile" | "desktop",            # optional, default "mobile"
    "max_wait_seconds": 90                        # optional, default 90, hard-cap 180
  }

Output:
  {
    "url": str,
    "final_url": str,
    "fetch_time": str,            # ISO-8601 from the report
    "strategy": str,
    "lighthouse_version": str,
    "scores": {
      "performance": int,         # 0-100, null if category disabled
      "accessibility": int,
      "best_practices": int,
      "seo": int,
      "pwa": int | null
    },
    "metrics": {
      "lcp_ms": int,              # Largest Contentful Paint
      "fcp_ms": int,              # First Contentful Paint
      "cls": float,               # Cumulative Layout Shift
      "tbt_ms": int,              # Total Blocking Time
      "tti_ms": int,              # Time to Interactive
      "speed_index_ms": int
    },
    "top_opportunities": [        # actionable perf wins, sorted by savings
      {"id": str, "title": str, "savings_ms": int, "description": str}
    ],
    "failed_audits": [            # non-passing audits across categories
      {"id": str, "category": str, "title": str, "score": float}
    ],
    "billing_units_actual": int   # always 1 (lighthouse is single-shot)
  }

Runtime:
  Requires `lighthouse` available on PATH (installed via `npm i -g lighthouse`
  in the Dockerfile). Chromium is reused from the Playwright install.
  No LLM. Pure subprocess + JSON parse.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_DEFAULT_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
_VALID_CATEGORIES = frozenset({"performance", "accessibility", "best-practices", "seo", "pwa"})
_VALID_STRATEGIES = frozenset({"mobile", "desktop"})
_DEFAULT_STRATEGY = "mobile"
_DEFAULT_TIMEOUT = 90
_MIN_TIMEOUT = 20
_MAX_TIMEOUT = 180
_MAX_OPPORTUNITIES = 8
_MAX_FAILED_AUDITS = 15
_STDERR_TAIL_CHARS = 600



def _score_to_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def _ms(raw: Any) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


def _resolve_lighthouse_bin() -> str | None:
    # Prefer an explicit env override (used in dev to point at a local install
    # without a global npm bin on PATH).
    override = os.environ.get("LIGHTHOUSE_BIN", "").strip()
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("lighthouse")


def _build_cmd(
    url: str, categories: list[str], strategy: str, output_path: str
) -> list[str]:
    chrome_flags = "--headless --no-sandbox --disable-gpu --disable-dev-shm-usage"
    return [
        _resolve_lighthouse_bin() or "lighthouse",
        url,
        "--quiet",
        "--output=json",
        f"--output-path={output_path}",
        f"--only-categories={','.join(categories)}",
        f"--form-factor={strategy}",
        "--throttling-method=simulate" if strategy == "mobile" else "--throttling-method=provided",
        f"--chrome-flags={chrome_flags}",
        # JSON only; we don't need the HTML report.
        "--max-wait-for-load=45000",
    ]


def _extract_top_opportunities(audits: dict) -> list[dict]:
    opps: list[dict] = []
    for audit_id, audit in audits.items():
        if not isinstance(audit, dict):
            continue
        details = audit.get("details") or {}
        if details.get("type") != "opportunity":
            continue
        savings = audit.get("numericValue") or details.get("overallSavingsMs") or 0
        try:
            savings_ms = int(round(float(savings)))
        except (TypeError, ValueError):
            savings_ms = 0
        if savings_ms <= 0:
            continue
        opps.append(
            {
                "id": audit_id,
                "title": str(audit.get("title") or audit_id),
                "savings_ms": savings_ms,
                "description": str(audit.get("description") or "")[:400],
            }
        )
    opps.sort(key=lambda item: item["savings_ms"], reverse=True)
    return opps[:_MAX_OPPORTUNITIES]


def _extract_failed_audits(report: dict) -> list[dict]:
    audits = report.get("audits") or {}
    cats = report.get("categories") or {}

    audit_to_categories: dict[str, list[str]] = {}
    for cat_id, cat in cats.items():
        if not isinstance(cat, dict):
            continue
        for ref in cat.get("auditRefs") or []:
            audit_id = ref.get("id")
            if audit_id:
                audit_to_categories.setdefault(audit_id, []).append(cat_id)

    failed: list[dict] = []
    for audit_id, audit in audits.items():
        if not isinstance(audit, dict):
            continue
        score = audit.get("score")
        # Lighthouse uses null for "informational" audits — skip those.
        if score is None:
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        if score_f >= 0.9:  # passing threshold
            continue
        category = (audit_to_categories.get(audit_id) or ["unknown"])[0]
        failed.append(
            {
                "id": audit_id,
                "category": category,
                "title": str(audit.get("title") or audit_id),
                "score": round(score_f, 2),
            }
        )
    failed.sort(key=lambda item: item["score"])
    return failed[:_MAX_FAILED_AUDITS]


def _normalize_run_inputs(
    payload: dict,
) -> dict | tuple[str, list[str], str, int]:
    """Pure: validate ``url``/``categories``/``strategy``/``max_wait_seconds``; returns parsed bag or error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("lighthouse_auditor.missing_url", "url is required")
    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("lighthouse_auditor.invalid_url", str(exc))
    raw_categories = payload.get("categories")
    if raw_categories is None or not isinstance(raw_categories, list) or not raw_categories:
        categories = list(_DEFAULT_CATEGORIES)
    else:
        categories = [str(c).strip().lower() for c in raw_categories if str(c).strip()]
        invalid = [c for c in categories if c not in _VALID_CATEGORIES]
        if invalid:
            return _err(
                "lighthouse_auditor.invalid_categories",
                f"Unsupported categories: {invalid}. Allowed: {sorted(_VALID_CATEGORIES)}",
            )
    strategy = str(payload.get("strategy") or _DEFAULT_STRATEGY).strip().lower()
    if strategy not in _VALID_STRATEGIES:
        return _err(
            "lighthouse_auditor.invalid_strategy",
            "strategy must be 'mobile' or 'desktop'",
        )
    try:
        timeout_s = int(payload.get("max_wait_seconds") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT
    return url, categories, strategy, max(_MIN_TIMEOUT, min(timeout_s, _MAX_TIMEOUT))


def _execute_lighthouse(
    url: str, categories: list[str], strategy: str, timeout_s: int, out_path: str,
) -> dict | None:
    """Side-effect: subprocess invoke; returns error envelope on failure or ``None`` on success."""
    cmd = _build_cmd(url, categories, strategy, out_path)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return _err(
            "lighthouse_auditor.timeout",
            f"Lighthouse exceeded {timeout_s}s; site may be unreachable or too slow.",
        )
    if proc.returncode != 0 and not os.path.exists(out_path):
        stderr_tail = (proc.stderr or "")[-_STDERR_TAIL_CHARS:]
        return _err(
            "lighthouse_auditor.run_failed",
            f"Lighthouse exited {proc.returncode}: {stderr_tail.strip() or 'no stderr'}",
        )
    return None


def _run_lighthouse_cli(
    url: str, categories: list[str], strategy: str, timeout_s: int,
) -> dict[str, Any]:
    """Side-effect: orchestrate one Lighthouse run; returns the report dict or error envelope."""
    if not _resolve_lighthouse_bin():
        return _err(
            "lighthouse_auditor.runtime_missing",
            "lighthouse CLI not found on PATH. Install with `npm i -g lighthouse` "
            "or set LIGHTHOUSE_BIN to its path. The production image installs it "
            "automatically; this only fires in stripped-down environments.",
        )
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = tmp.name
    try:
        err = _execute_lighthouse(url, categories, strategy, timeout_s, out_path)
        if err is not None:
            return err
        try:
            with open(out_path, encoding="utf-8") as fp:
                return json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            return _err(
                "lighthouse_auditor.parse_failed",
                f"Could not parse Lighthouse JSON output: {exc}",
            )
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _audit_num(audits: dict[str, Any], audit_id: str) -> Any:
    """Pure: pull ``numericValue`` from a lighthouse audit, ``None`` if absent."""
    a = audits.get(audit_id)
    return a.get("numericValue") if isinstance(a, dict) else None


def _shape_lighthouse_report(
    *, url: str, strategy: str, report: dict[str, Any],
) -> dict[str, Any]:
    """Pure: project a parsed lighthouse JSON report into the agent's response shape."""
    cats = report.get("categories") or {}
    scores = {
        "performance": _score_to_int((cats.get("performance") or {}).get("score")),
        "accessibility": _score_to_int((cats.get("accessibility") or {}).get("score")),
        "best_practices": _score_to_int((cats.get("best-practices") or {}).get("score")),
        "seo": _score_to_int((cats.get("seo") or {}).get("score")),
        "pwa": _score_to_int((cats.get("pwa") or {}).get("score")),
    }
    audits = report.get("audits") or {}
    metrics = {
        "lcp_ms": _ms(_audit_num(audits, "largest-contentful-paint")),
        "fcp_ms": _ms(_audit_num(audits, "first-contentful-paint")),
        "cls": round(float(_audit_num(audits, "cumulative-layout-shift") or 0.0), 3),
        "tbt_ms": _ms(_audit_num(audits, "total-blocking-time")),
        "tti_ms": _ms(_audit_num(audits, "interactive")),
        "speed_index_ms": _ms(_audit_num(audits, "speed-index")),
    }
    return {
        "url": url,
        "final_url": str(report.get("finalUrl") or report.get("finalDisplayedUrl") or url),
        "fetch_time": str(report.get("fetchTime") or ""),
        "strategy": strategy,
        "lighthouse_version": str(report.get("lighthouseVersion") or ""),
        "scores": scores,
        "metrics": metrics,
        "top_opportunities": _extract_top_opportunities(audits),
        "failed_audits": _extract_failed_audits(report),
        "billing_units_actual": 1,
    }


def run(payload: dict) -> dict:
    """Run Google Lighthouse against a public URL with headless Chromium.

    Why: a single-shot CLI invocation gives canonical category scores +
    web-vitals; we cap wall-time and tail stderr so a misbehaving page
    can't hang the worker or hide its failure from the caller.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    url, categories, strategy, timeout_s = parsed
    report = _run_lighthouse_cli(url, categories, strategy, timeout_s)
    if "error" in report:
        return report  # error envelope
    return _shape_lighthouse_report(url=url, strategy=strategy, report=report)
