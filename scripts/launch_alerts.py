#!/usr/bin/env python3
"""Launch-readiness alert evaluator for Aztea.

Polls a small set of operational endpoints and turns the raw metrics into a
structured alert report:

  - failed_call_spike   — % of recently-failed jobs above threshold
  - refund_spike        — refund-to-charge ratio above threshold
  - ledger_drift        — reconciliation reports non-zero drift
  - degraded_agents     — public agents with low success rate or stale activity
  - search_empty        — /registry/search returns zero results for a known query
  - worker_backlog      — pending job count above threshold

The pure-evaluation functions (``evaluate_*``) take dicts and return Alert
records, which makes the rules unit-testable without HTTP. ``main()`` wires
the rules to live endpoints; CI / cron should run that and treat any non-zero
exit as a launch blocker.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import requests


@dataclass
class Alert:
    name: str
    severity: str  # "info" | "warn" | "critical"
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Thresholds:
    failed_call_pct_warn: float = 0.05  # 5%
    failed_call_pct_critical: float = 0.15  # 15%
    refund_ratio_warn: float = 0.10  # 10% of charges
    refund_ratio_critical: float = 0.25  # 25% of charges
    ledger_drift_cents_warn: int = 1
    ledger_drift_cents_critical: int = 100
    agent_success_warn: float = 0.80
    agent_success_critical: float = 0.50
    worker_backlog_warn: int = 25
    worker_backlog_critical: int = 100
    min_today_jobs_for_eval: int = 10  # don't fire if sample is too small


def _ratio(numer: float, denom: float) -> float:
    if denom <= 0:
        return 0.0
    return float(numer) / float(denom)


def evaluate_failed_call_spike(
    jobs_metrics: dict[str, Any], thresholds: Thresholds
) -> list[Alert]:
    total = int(jobs_metrics.get("total_today") or jobs_metrics.get("total") or 0)
    failed = int(jobs_metrics.get("failed_today") or jobs_metrics.get("failed") or 0)
    if total < thresholds.min_today_jobs_for_eval:
        return []
    pct = _ratio(failed, total)
    detail = {"failed": failed, "total": total, "pct": round(pct, 4)}
    if pct >= thresholds.failed_call_pct_critical:
        return [
            Alert(
                "failed_call_spike",
                "critical",
                f"{failed}/{total} jobs failed today ({pct:.1%})",
                detail,
            )
        ]
    if pct >= thresholds.failed_call_pct_warn:
        return [
            Alert(
                "failed_call_spike",
                "warn",
                f"{failed}/{total} jobs failed today ({pct:.1%})",
                detail,
            )
        ]
    return []


def evaluate_refund_spike(
    payments: dict[str, Any], thresholds: Thresholds
) -> list[Alert]:
    charges = int(payments.get("charges_cents_today") or 0)
    refunds = int(payments.get("refunds_cents_today") or 0)
    if charges < thresholds.min_today_jobs_for_eval:
        return []
    ratio = _ratio(refunds, charges)
    detail = {
        "refunds_cents": refunds,
        "charges_cents": charges,
        "ratio": round(ratio, 4),
    }
    if ratio >= thresholds.refund_ratio_critical:
        return [
            Alert(
                "refund_spike",
                "critical",
                f"refund ratio {ratio:.1%} (refunds {refunds}c / charges {charges}c)",
                detail,
            )
        ]
    if ratio >= thresholds.refund_ratio_warn:
        return [
            Alert(
                "refund_spike",
                "warn",
                f"refund ratio {ratio:.1%} (refunds {refunds}c / charges {charges}c)",
                detail,
            )
        ]
    return []


def evaluate_ledger_drift(
    reconcile: dict[str, Any], thresholds: Thresholds
) -> list[Alert]:
    drift = int(abs(reconcile.get("drift_cents") or 0))
    mismatches = int(reconcile.get("mismatch_count") or 0)
    detail = {"drift_cents": drift, "mismatch_count": mismatches}
    if drift >= thresholds.ledger_drift_cents_critical or mismatches > 0:
        return [
            Alert(
                "ledger_drift",
                "critical",
                f"reconciliation drift {drift}c, mismatches {mismatches}",
                detail,
            )
        ]
    if drift >= thresholds.ledger_drift_cents_warn:
        return [Alert("ledger_drift", "warn", f"reconciliation drift {drift}c", detail)]
    return []


def evaluate_degraded_agents(
    agents: Iterable[dict[str, Any]], thresholds: Thresholds
) -> list[Alert]:
    out: list[Alert] = []
    for agent in agents or []:
        name = str(agent.get("name") or agent.get("agent_id") or "?")
        success = agent.get("success_rate")
        total_calls = int(agent.get("total_calls") or 0)
        if success is None or total_calls < thresholds.min_today_jobs_for_eval:
            continue
        try:
            success_f = float(success)
        except (TypeError, ValueError):
            continue
        detail = {"agent": name, "success_rate": success_f, "total_calls": total_calls}
        if success_f <= thresholds.agent_success_critical:
            out.append(
                Alert(
                    "degraded_agent",
                    "critical",
                    f"{name} success {success_f:.1%} over {total_calls} calls",
                    detail,
                )
            )
        elif success_f <= thresholds.agent_success_warn:
            out.append(
                Alert(
                    "degraded_agent",
                    "warn",
                    f"{name} success {success_f:.1%} over {total_calls} calls",
                    detail,
                )
            )
    return out


def evaluate_search_empty(probe_results: dict[str, Any]) -> list[Alert]:
    out: list[Alert] = []
    for query, count in (probe_results or {}).items():
        if int(count) <= 0:
            out.append(
                Alert(
                    "search_empty",
                    "warn",
                    f"search returned 0 results for {query!r}",
                    {"query": query},
                )
            )
    return out


def evaluate_worker_backlog(
    jobs_metrics: dict[str, Any], thresholds: Thresholds
) -> list[Alert]:
    pending = int(jobs_metrics.get("pending") or 0)
    detail = {"pending": pending}
    if pending >= thresholds.worker_backlog_critical:
        return [
            Alert(
                "worker_backlog", "critical", f"{pending} pending jobs in queue", detail
            )
        ]
    if pending >= thresholds.worker_backlog_warn:
        return [
            Alert("worker_backlog", "warn", f"{pending} pending jobs in queue", detail)
        ]
    return []


# ─── HTTP wiring ─────────────────────────────────────────────────────────────

_PROBE_QUERIES = [
    "lint python code",
    "type check python",
    "scan secrets",
    "audit dependencies",
]


def _get(
    base: str, path: str, headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    resp = requests.get(f"{base}{path}", headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _post(
    base: str, path: str, headers: dict[str, str], body: Any, timeout: float
) -> dict[str, Any]:
    resp = requests.post(f"{base}{path}", headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _collect_metrics(base: str, api_key: str, timeout: float) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "X-Aztea-Client": "launch-alerts"}
    metrics: dict[str, Any] = {}
    metrics["jobs"] = _get(base, "/ops/jobs/metrics", headers, timeout)
    metrics["payments"] = _get(base, "/ops/platform-stats", headers, timeout)
    metrics["reconcile"] = _post(base, "/ops/payments/reconcile", headers, {}, timeout)
    probe: dict[str, int] = {}
    for q in _PROBE_QUERIES:
        try:
            payload = _post(
                base, "/registry/search", headers, {"query": q, "limit": 5}, timeout
            )
            probe[q] = len(payload.get("results") or [])
        except Exception:
            probe[q] = 0
    metrics["search_probes"] = probe
    metrics["agents"] = (_get(base, "/registry/agents", headers, timeout) or {}).get(
        "agents"
    ) or []
    return metrics


def evaluate_all(
    metrics: dict[str, Any], thresholds: Thresholds | None = None
) -> list[Alert]:
    thresholds = thresholds or Thresholds()
    alerts: list[Alert] = []
    alerts += evaluate_failed_call_spike(metrics.get("jobs") or {}, thresholds)
    alerts += evaluate_refund_spike(metrics.get("payments") or {}, thresholds)
    alerts += evaluate_ledger_drift(metrics.get("reconcile") or {}, thresholds)
    alerts += evaluate_degraded_agents(metrics.get("agents") or [], thresholds)
    alerts += evaluate_search_empty(metrics.get("search_probes") or {})
    alerts += evaluate_worker_backlog(metrics.get("jobs") or {}, thresholds)
    return alerts


def main(argv: list[str] | None = None) -> int:
    base = os.environ.get("AZTEA_BASE_URL", "http://localhost:8000").rstrip("/")
    api_key = os.environ.get("AZTEA_API_KEY", "").strip()
    timeout = float(os.environ.get("AZTEA_ALERTS_TIMEOUT", "10"))
    if not api_key:
        print(
            json.dumps({"error": "AZTEA_API_KEY required for /ops endpoints"}),
            file=sys.stderr,
        )
        return 2
    try:
        metrics = _collect_metrics(base, api_key, timeout)
    except Exception as exc:
        print(
            json.dumps({"error": f"metrics collection failed: {exc}"}), file=sys.stderr
        )
        return 2
    alerts = evaluate_all(metrics)
    report = {"base_url": base, "alerts": [asdict(a) for a in alerts]}
    print(json.dumps(report, indent=2))
    if any(a.severity == "critical" for a in alerts):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
