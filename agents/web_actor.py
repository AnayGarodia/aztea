"""web_actor.py — escrowed write-web actions (Phase 4). Fail-closed.

A SEPARATE agent from the read-only site_navigator: a prompt-injected read path
can never reach a commit, because the read agent has no commit code at all
(defense in depth — the magnet agent literally cannot spend money).

Every path is OFF unless ops flips the kill switches:
  AZTEA_ACTION_WEB_ENABLED         master (preview + commit)
  AZTEA_ACTION_WEB_COMMIT_ENABLED  the consequential commit step

Actions:
  - interact (E1, SAFE): a bounded content-revealing interaction sequence
    (click/fill/select/scroll/wait), then return the revealed page. No mandate, no
    money, no credentials — gated only by the master switch.
  - preview / commit (E2+, escrowed): caller creates an action mandate (bounded cap,
    allowed domains, expiry, single-use nonce) -> preview (legible confirmation, no
    money) -> authorizes with the nonce -> commit (consumes the mandate under cap +
    gating). The real-world irreversible submit and the live escrow ledger settlement
    are the deferred write-web money-PR; this ships the safety + lifecycle scaffold.

Input:
  {"action": "interact", "url": str,
   "steps": [{"action": "click|fill|select|scroll|wait", "target": str, "value": str}]}
  {"action": "preview"|"commit", "url": str, "mandate_id": str,
   "confirmation_nonce": str (commit only)}
"""

from __future__ import annotations

import json
import logging
from typing import Any

from urllib.parse import urlparse

from agents import _web_interact
from agents._contracts import agent_error as _err
from core import action_mandates, feature_flags, url_security

_LOG = logging.getLogger(__name__)
_VALID_ACTIONS = ("interact", "preview", "commit")


def _import_playwright() -> Any:
    """Side-effect: lazy Playwright import (heavy; only the interact path needs it)."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
        return sync_playwright
    except ImportError:
        return _err(
            "web_actor.tool_unavailable",
            "playwright is not installed on this executor. Install it with: "
            "pip install playwright && playwright install chromium",
        )


def _url_within_allowed_domains(url: str, mandate: dict[str, Any]) -> bool:
    """Pure: the action url's host must equal one of the mandate's allowed_domains, or
    be a subdomain of one. A mandate with no allowed_domains authorizes nothing (the
    domain binding, actually enforced — not just echoed in preview)."""
    allowed = [str(d or "").strip().lower() for d in _domains(mandate.get("allowed_domains")) if d]
    host = (urlparse(url).hostname or "").strip().lower()
    return bool(host) and any(host == d or host.endswith("." + d) for d in allowed)


def _domains(raw: Any) -> list[str]:
    """Pure: decode the mandate's stored allowed_domains JSON to a list."""
    if isinstance(raw, list):
        return raw
    try:
        decoded = json.loads(raw or "[]")
        return decoded if isinstance(decoded, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _preview(mandate: dict[str, Any]) -> dict[str, Any]:
    """Read-only: return the legible confirmation struct so a human/agent can decide.

    Surfaces the danger up front (what action, what it can cost, reversibility,
    which domains) rather than burying it in a signed blob. Moves no money, takes
    no action. (A live render-based preview is part of the deferred money-PR.)
    """
    return {
        "phase": "previewed",
        "confirmation": {
            "mandate_id": mandate["mandate_id"],
            "action_kind": mandate["action_kind"],
            "reversibility": mandate["reversibility"],
            "max_spend_cents": int(mandate["max_spend_cents"]),
            "currency": mandate.get("currency", "USD"),
            "allowed_domains": _domains(mandate.get("allowed_domains")),
        },
        "next_step": "authorize the mandate with its confirmation_nonce, then call action='commit'.",
        "degraded_mode": False,
    }


def _commit(mandate: dict[str, Any], nonce: str) -> dict[str, Any]:
    """Consume an authorized mandate under cap + commit gating.

    The consume is atomic (rowcount guard in action_mandates) and is the
    idempotency key: a replay finds the mandate already consumed and does nothing.
    """
    if not feature_flags.action_web_commit_enabled():
        return _err(
            "web_actor.commit_disabled",
            "Commit is disabled (preview-only mode). Set AZTEA_ACTION_WEB_COMMIT_ENABLED=1. "
            "No action taken.",
        )
    if not nonce:
        return _err("web_actor.missing_nonce", "confirmation_nonce is required to commit.")
    if not action_mandates.consume_mandate(mandate["mandate_id"], nonce):
        return _err(
            "web_actor.not_authorized",
            "Mandate is not authorized with a matching nonce (already used, revoked, "
            "expired, or never confirmed). No action taken.",
        )
    # Safety scaffold complete: mandate consumed under cap + kill-switch gating.
    # The real-world irreversible submit and live escrow ledger settlement land in
    # the focused write-web money-PR (must not share a release train with reads).
    return {
        "phase": "committed_validated",
        "mandate_id": mandate["mandate_id"],
        "action_kind": mandate["action_kind"],
        "max_spend_cents": int(mandate["max_spend_cents"]),
        "execution": "deferred",
        "note": "Mandate consumed under cap + gating. Live action + escrow settlement "
                "land in the write-web money-PR.",
        "degraded_mode": False,
    }


def _interact(url: str, raw_steps: Any) -> dict[str, Any]:
    """E1 (safe tier): a bounded content-revealing interaction sequence, then return
    the revealed page. No mandate, no money — gated only by the master kill switch, so
    it ships OFF with the rest of the write-web. The interaction code lives here, never
    in the read agent, so a coerced read path still cannot act.
    """
    if not url:
        return _err("web_actor.missing_url", "url is required for the interact action.")
    try:
        steps = _web_interact.parse_steps(raw_steps)
    except ValueError as exc:
        return _err("web_actor.invalid_steps", str(exc))
    sync_playwright = _import_playwright()
    if isinstance(sync_playwright, dict):
        return sync_playwright  # error envelope
    try:
        with sync_playwright() as pw:
            outcome = _web_interact.perform_interaction(pw, url, steps)
    except Exception as exc:  # noqa: BLE001 — uniform envelope so settlement refunds
        return _err("web_actor.interaction_failed", f"Interaction failed: {type(exc).__name__}: {exc}")
    if isinstance(outcome, dict) and outcome.get("final_url"):
        # Defense-in-depth: re-validate the post-interaction final URL before trusting it.
        try:
            url_security.validate_outbound_url(outcome["final_url"], "url")
        except Exception as exc:  # noqa: BLE001 — structured envelope
            return _err("web_actor.url_blocked", f"final URL after interaction is blocked: {exc}")
    return outcome


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a write-web action. Fail-closed: disabled unless ops opts in.

    Why a separate agent and not a mode of site_navigator: keeping the commit
    code out of the read agent makes "a coerced read agent cannot spend money"
    a structural guarantee, not a runtime check.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    if not feature_flags.action_web_enabled():
        return _err(
            "web_actor.disabled",
            "The escrowed write-web is disabled (AZTEA_ACTION_WEB_ENABLED=0). No action taken.",
        )
    action = str(payload.get("action") or "").strip().lower()
    if action not in _VALID_ACTIONS:
        return _err("web_actor.invalid_action", f"action must be one of: {', '.join(_VALID_ACTIONS)}")
    raw_url = str(payload.get("url") or "").strip()
    if raw_url:
        try:
            url_security.validate_outbound_url(raw_url, "url")
        except Exception as exc:  # noqa: BLE001 — structured envelope
            return _err("web_actor.url_blocked", str(exc))
    # E1 — interact-then-reveal: the safe tier, no mandate/money (master switch only).
    if action == "interact":
        return _interact(raw_url, payload.get("steps"))
    mandate_id = str(payload.get("mandate_id") or "").strip()
    if not mandate_id:
        return _err("web_actor.missing_mandate", "mandate_id is required.")
    mandate = action_mandates.get_mandate(mandate_id)
    if mandate is None:
        return _err("web_actor.mandate_not_found", "No such mandate.")
    if action == "preview":
        return _preview(mandate)
    # The mandate's domain binding, actually enforced at commit: refuse an action url
    # outside the authorized scope, so a coerced/mistaken URL can't be acted on.
    if not _url_within_allowed_domains(raw_url, mandate):
        return _err(
            "web_actor.domain_not_allowed",
            "the action url is not within the mandate's allowed_domains. No action taken.",
        )
    return _commit(mandate, str(payload.get("confirmation_nonce") or "").strip())
