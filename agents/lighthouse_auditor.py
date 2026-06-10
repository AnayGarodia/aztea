"""
lighthouse_auditor.py — Run Google Lighthouse against any public URL.

Input:
  {
    "url": "https://example.com",                # required
    "categories": ["performance", "accessibility",
                   "best-practices", "seo", "pwa"],  # optional, default first 4
    "strategy": "mobile" | "desktop",            # optional, default "mobile"
    "throttling": "simulate"|"provided"|"devtools",  # optional; default derives from strategy
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

import glob
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
# 2026-05-18: bumped from 90 → 150s. Lighthouse cold-start + chromium
# warmup + the full audit pass routinely takes 75–110s on the prod host;
# the previous 90s default tripped `lighthouse_auditor.timeout` on roughly
# every real-world commercial page in the test report. The hard ceiling
# stays at 240s to bound wall-clock per call.
_DEFAULT_TIMEOUT = 180
_MIN_TIMEOUT = 30
_MAX_TIMEOUT = 300
# Lighthouse's own ``--max-wait-for-load`` ceiling. Bumped to 90s on
# 2026-05-20 alongside an outer-timeout bump: slow LCP pages (real-world
# commercial sites with heavy frameworks + cold CDN cache) routinely take
# 65–80s on the first paint of a fresh chromium launch and lighthouse's
# own gate was firing before subprocess.run could. Keeping the bump
# strictly below ``_DEFAULT_TIMEOUT`` so lighthouse fails its own gate
# before the outer kill.
_LIGHTHOUSE_MAX_WAIT_MS = 90_000
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


# Playwright caches chromium under a version-suffixed directory
# (e.g. ``chromium-1208``) which rolls forward when Playwright is upgraded.
# We resolve at call time rather than baking a path so an upgrade doesn't
# silently re-break the agent.
_PLAYWRIGHT_CACHE_DIRS = (
    "/home/aztea/.cache/ms-playwright",
    os.path.expanduser("~/.cache/ms-playwright"),
)


def _resolve_chrome_path() -> str | None:
    """Pure-ish: locate a Chrome/Chromium binary lighthouse can launch.

    Why: lighthouse uses chrome-launcher, which only searches a fixed list
    of well-known install paths (``/usr/bin/google-chrome`` etc.). The
    Aztea production host runs Playwright's bundled chromium under
    ``~/.cache/ms-playwright/chromium-<version>/``, which chrome-launcher
    never discovers — so every lighthouse call dies with
    ``ChromePathNotSetError`` and an empty output file (2026-05-18 test
    report). Resolve a usable binary here and we pass it through CHROME_PATH
    in the subprocess env; lighthouse honours that without further config.
    """
    override = os.environ.get("CHROME_PATH", "").strip()
    if override and os.path.isfile(override):
        return override
    for system_path in ("google-chrome", "chromium-browser", "chromium", "chrome"):
        which = shutil.which(system_path)
        if which:
            return which
    # First try the known Playwright glob patterns — fast and covers 99%
    # of installs. If those miss (Playwright reorganised the cache layout,
    # the version is uncommon, etc.), fall back to a full os.walk of the
    # cache directories looking for any executable named chrome/chromium.
    for cache_dir in _PLAYWRIGHT_CACHE_DIRS:
        for pattern in (
            f"{cache_dir}/chromium-*/chrome-linux64/chrome",
            f"{cache_dir}/chromium-*/chrome-linux/chrome",
            f"{cache_dir}/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        ):
            matches = sorted(glob.glob(pattern))
            if matches:
                # Newest version sorts last alphanumerically (chromium-1208 < chromium-1210).
                return matches[-1]
    # Last-resort exhaustive walk for hosts where Playwright has changed
    # the directory layout out from under us. Bounded depth keeps this
    # cheap even when the cache has many sibling installs.
    for cache_dir in _PLAYWRIGHT_CACHE_DIRS:
        if not os.path.isdir(cache_dir):
            continue
        candidates: list[str] = []
        for root, dirs, files in os.walk(cache_dir):
            depth = root[len(cache_dir):].count(os.sep)
            if depth > 4:
                dirs.clear()
                continue
            for fname in files:
                if fname in ("chrome", "chromium", "chrome-headless-shell"):
                    full = os.path.join(root, fname)
                    if os.access(full, os.X_OK):
                        candidates.append(full)
        if candidates:
            # Prefer the full chrome binary over the headless-shell variant.
            candidates.sort(key=lambda p: (p.endswith("headless-shell"), p))
            return candidates[0]
    return None


# --throttling-method values lighthouse accepts. "" (unset) derives the
# method from the strategy, preserving the agent's historical behavior:
# mobile simulates 4G, desktop uses the connection as-is ("provided").
_VALID_THROTTLING = ("", "simulate", "provided", "devtools")


def _build_cmd(
    url: str, categories: list[str], strategy: str, output_path: str,
    throttling: str,
) -> list[str]:
    # 2026-05-18: extra chrome flags reduce hang-on-cold-start on the prod
    # host. ``--ignore-certificate-errors`` lets us audit sites with stale or
    # self-signed certs (which previously surfaced as RUNTIME_ERROR mid-run);
    # ``--disable-extensions`` and ``--disable-default-apps`` shave a few
    # seconds of chromium warmup; ``--disable-background-networking`` keeps
    # chromium from racing the audit with metrics uploads.
    chrome_flags = (
        "--headless "
        "--no-sandbox "
        "--disable-gpu "
        "--disable-dev-shm-usage "
        "--ignore-certificate-errors "
        "--disable-extensions "
        "--disable-default-apps "
        "--disable-background-networking"
    )
    if not throttling:
        throttling = "simulate" if strategy == "mobile" else "provided"
    return [
        _resolve_lighthouse_bin() or "lighthouse",
        url,
        "--quiet",
        "--output=json",
        f"--output-path={output_path}",
        f"--only-categories={','.join(categories)}",
        f"--form-factor={strategy}",
        f"--throttling-method={throttling}",
        f"--chrome-flags={chrome_flags}",
        # JSON only; we don't need the HTML report.
        f"--max-wait-for-load={_LIGHTHOUSE_MAX_WAIT_MS}",
    ]


def _coerce_savings(value: Any) -> int:
    """Pure: best-effort numeric → non-negative int; garbage becomes 0."""
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _opportunity_savings_ms(audit: dict, details: dict) -> int:
    """Pure: estimated ms saved by one opportunity audit.

    Newer Lighthouse (12+) drops numericValue on some opportunities and
    ships per-metric estimates under ``metricSavings`` instead — take the
    largest of those when the classic keys are absent.
    """
    classic = _coerce_savings(
        audit.get("numericValue") or details.get("overallSavingsMs")
    )
    if classic > 0:
        return classic
    metric_savings = audit.get("metricSavings")
    if isinstance(metric_savings, dict) and metric_savings:
        return max(_coerce_savings(v) for v in metric_savings.values())
    return 0


def _extract_top_opportunities(audits: dict) -> list[dict]:
    opps: list[dict] = []
    for audit_id, audit in audits.items():
        if not isinstance(audit, dict):
            continue
        details = audit.get("details")
        if not isinstance(details, dict) or details.get("type") != "opportunity":
            continue
        savings_ms = _opportunity_savings_ms(audit, details)
        savings_bytes = _coerce_savings(details.get("overallSavingsBytes"))
        if savings_ms <= 0 and savings_bytes <= 0:
            continue
        opps.append(
            {
                "id": audit_id,
                "title": str(audit.get("title") or audit_id),
                "savings_ms": savings_ms,
                "savings_bytes": savings_bytes,
                "description": str(audit.get("description") or "")[:400],
            }
        )
    # ms first; bytes break ties for byte-only wins (e.g. image formats).
    opps.sort(key=lambda item: (item["savings_ms"], item["savings_bytes"]), reverse=True)
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
    if not isinstance(raw_categories, list) or not raw_categories:
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
    throttling = str(payload.get("throttling") or "").strip().lower()
    if throttling not in _VALID_THROTTLING:
        return _err(
            "lighthouse_auditor.invalid_throttling",
            "throttling must be one of: simulate, provided, devtools "
            "(omit to derive from strategy)",
        )
    try:
        timeout_s = int(payload.get("max_wait_seconds") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT
    return url, categories, strategy, throttling, max(_MIN_TIMEOUT, min(timeout_s, _MAX_TIMEOUT))


def _report_has_content(out_path: str) -> bool:
    """Pure: ``True`` when ``out_path`` exists with non-zero size.

    Defensive against test harnesses that patch ``os.path.exists`` but not
    ``os.path.getsize`` — falls back to "exists" if getsize raises.
    """
    if not os.path.exists(out_path):
        return False
    try:
        return os.path.getsize(out_path) > 0
    except OSError:
        return True


def _execute_lighthouse(
    url: str, categories: list[str], strategy: str, throttling: str,
    timeout_s: int, out_path: str,
) -> dict | None:
    """Side-effect: subprocess invoke; returns error envelope on failure or ``None`` on success."""
    cmd = _build_cmd(url, categories, strategy, out_path, throttling)
    env = os.environ.copy()
    chrome_path = _resolve_chrome_path()
    if chrome_path:
        # See ``_resolve_chrome_path`` docstring for why this is essential
        # on hosts that ship chromium under Playwright's cache.
        env["CHROME_PATH"] = chrome_path
    # Note: we intentionally do NOT short-circuit when chrome_path is None.
    # lighthouse may still find a system chrome that our resolver missed,
    # and tests that mock subprocess.run need the pipeline to proceed. The
    # post-run path below surfaces the structured chromium_unavailable
    # envelope when the subprocess actually fails for that reason.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return _err(
            "lighthouse_auditor.timeout",
            f"Lighthouse exceeded {timeout_s}s; site may be unreachable or too slow.",
        )
    # 2026-05-18: lighthouse can exit non-zero AND still write a partial
    # report containing a ``runtimeError`` describing the failure (e.g.
    # NO_FCP, FAILED_DOCUMENT_REQUEST). Only treat returncode!=0 AND no
    # output file as a hard infra failure; everything else falls through to
    # the JSON parser + runtimeError surfacing so callers see the real
    # cause rather than a generic timeout.
    if proc.returncode != 0 and not _report_has_content(out_path):
        stderr_tail = (proc.stderr or "")[-_STDERR_TAIL_CHARS:]
        # When no chromium was discoverable, surface that as its own error
        # code so callers can route to the install instructions instead of
        # debugging a generic "lighthouse run failed".
        if not chrome_path:
            return _err(
                "lighthouse_auditor.chromium_unavailable",
                "Lighthouse cannot run: no Chrome/Chromium binary discoverable "
                "on this worker. Set CHROME_PATH, install google-chrome / "
                "chromium-browser, or run `playwright install chromium`. "
                f"Lighthouse exited {proc.returncode}; stderr tail: "
                f"{stderr_tail.strip() or 'no stderr'}.",
                {"searched_paths": list(_PLAYWRIGHT_CACHE_DIRS)},
            )
        return _err(
            "lighthouse_auditor.run_failed",
            f"Lighthouse exited {proc.returncode} with no JSON output: "
            f"{stderr_tail.strip() or 'no stderr'}.",
        )
    return None


def _run_lighthouse_cli(
    url: str, categories: list[str], strategy: str, throttling: str, timeout_s: int,
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
        err = _execute_lighthouse(url, categories, strategy, throttling, timeout_s, out_path)
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
    url, categories, strategy, throttling, timeout_s = parsed
    report = _run_lighthouse_cli(url, categories, strategy, throttling, timeout_s)
    if "error" in report:
        return report  # error envelope
    # 2026-05-18: lighthouse can return a JSON report whose top-level
    # ``runtimeError`` describes why the audit failed (NO_FCP, PROTOCOL_TIMEOUT,
    # FAILED_DOCUMENT_REQUEST). Surface that explicitly instead of returning
    # a structurally-valid report with all-null metrics, which the test report
    # flagged as a misleading "successful" call.
    runtime_error = report.get("runtimeError") if isinstance(report, dict) else None
    if isinstance(runtime_error, dict) and runtime_error.get("code"):
        return _err(
            "lighthouse_auditor.runtime_error",
            f"Lighthouse reported runtimeError {runtime_error.get('code')}: "
            f"{runtime_error.get('message') or 'no message'}",
            {"runtime_error": runtime_error, "url": url, "strategy": strategy},
        )
    return _shape_lighthouse_report(url=url, strategy=strategy, report=report)
