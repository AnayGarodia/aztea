"""web_actor.py — escrowed write-web actions (Phase 4). Fail-closed.

The ACTION ENGINE behind the unified Web Agent (agents/site_navigator): site_navigator
delegates interact / preview / dry_run / commit calls here. It is no longer a separate
listed agent (the 2026-06 merge to one agent). The read/write isolation is therefore a
RUNTIME-FLAG guarantee now, not a structural one — every write path is OFF until an
operator flips the kill switches below, so a default deploy of the merged agent only
reads. (Turning the write web on for a public deploy wants a /cso pass — that promise
moved from "structural" to "flag-gated" with this merge.)

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
from core import action_mandates, credential_vault, feature_flags, url_security
from core.web import stealth_browser

_LOG = logging.getLogger(__name__)
_VALID_ACTIONS = ("interact", "preview", "dry_run", "commit")


def _import_playwright() -> Any:
    """Side-effect: lazy Playwright import (heavy; only the interact path needs it).

    Routed through stealth_browser so AZTEA_STEALTH_BROWSER selects the undetected
    (patchright) build; default OFF returns stock Playwright unchanged.
    """
    try:
        return stealth_browser.playwright_module()
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


def _run_interaction(url: str, steps: list[dict[str, str]], credential: Any) -> dict[str, Any]:
    """Side-effect: open a stealth-aware Playwright session, drive the bounded steps,
    and return the revealed page (or a structured error). Shared by interact + dry_run.

    When a credential is supplied (the gated dry-run-with-login path) it is injected,
    and is ALWAYS scrubbed in the finally — even on an early error return. Re-validates
    the post-interaction final URL as defense-in-depth.
    """
    try:
        sync_playwright = _import_playwright()
        if isinstance(sync_playwright, dict):
            return sync_playwright  # error envelope
        try:
            with sync_playwright() as pw:
                outcome = (
                    _web_interact.perform_login(pw, url, credential, steps)
                    if credential is not None
                    else _web_interact.perform_interaction(pw, url, steps)
                )
        except Exception as exc:  # noqa: BLE001 — uniform envelope so settlement refunds
            return _err("web_actor.interaction_failed", f"Interaction failed: {type(exc).__name__}: {exc}")
        if isinstance(outcome, dict) and outcome.get("final_url"):
            try:
                url_security.validate_outbound_url(outcome["final_url"], "url")
            except Exception as exc:  # noqa: BLE001 — structured envelope
                return _err("web_actor.url_blocked", f"final URL after interaction is blocked: {exc}")
        return outcome
    finally:
        if credential is not None:
            credential.scrub()


def _interact(url: str, raw_steps: Any) -> dict[str, Any]:
    """E1 (safe tier): a bounded content-revealing interaction sequence, then return
    the revealed page. No mandate, no money, no credentials — gated only by the master
    kill switch, so it ships OFF with the rest of the write-web. The interaction code
    lives here, never in the read agent, so a coerced read path still cannot act.
    """
    if not url:
        return _err("web_actor.missing_url", "url is required for the interact action.")
    try:
        steps = _web_interact.parse_steps(raw_steps)
    except ValueError as exc:
        return _err("web_actor.invalid_steps", str(exc))
    return _run_interaction(url, steps, None)


def _resolve_credential(url: str, mandate: dict[str, Any], cred_kind: str) -> Any:
    """Resolve a vault credential for a gated dry-run-with-login. Returns a Credential,
    or a structured error envelope when injection is disabled/unavailable or none is
    stored. The owner is taken from the mandate, so a caller can only reach their own
    credentials and only on a domain the mandate authorizes.
    """
    host = (urlparse(url).hostname or "").strip().lower()
    try:
        cred = credential_vault._decrypt_for_injection(
            owner_id=str(mandate.get("caller_owner_id") or ""),
            domain=host, cred_kind=cred_kind, mandate=mandate,
        )
    except credential_vault.VaultUnavailable as exc:
        return _err("web_actor.credential_unavailable", str(exc))
    except credential_vault.VaultError as exc:
        return _err("web_actor.credential_error", str(exc))
    if cred is None:
        return _err(
            "web_actor.no_credential",
            "no stored credential for this owner/domain/kind. No action taken.",
        )
    return cred


def _dry_run(url: str, mandate: dict[str, Any], raw_steps: Any, use_credential_kind: str) -> dict[str, Any]:
    """Rehearsal: navigate + apply the (optional, non-submitting) steps and return the
    revealed page alongside the mandate's planned outcome — WITHOUT consuming the
    mandate or performing the irreversible commit.

    The honest "here is exactly what a commit would do" surface. Needs only the master
    switch (no commit flag), because it moves nothing. When use_credential_kind is set
    AND injection is enabled, it logs in first (the credential is fetched after step
    validation and always scrubbed inside _run_interaction).
    """
    steps: list[dict[str, str]] = []
    if raw_steps:
        try:
            steps = _web_interact.parse_steps(raw_steps)
        except ValueError as exc:
            return _err("web_actor.invalid_steps", str(exc))
    credential: Any = None
    if use_credential_kind:
        credential = _resolve_credential(url, mandate, use_credential_kind)
        if isinstance(credential, dict) and "error" in credential:
            return credential
    revealed = _run_interaction(url, steps, credential)
    if isinstance(revealed, dict) and "error" in revealed:
        return revealed
    return {
        "phase": "dry_run",
        "planned": _preview(mandate)["confirmation"],
        "revealed": revealed,
        "note": "Rehearsal only: the mandate was NOT consumed and no action was taken. "
                "Authorize the mandate, then call action='commit' to perform it.",
        "degraded_mode": bool(revealed.get("degraded_mode", False)),
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a write-web action. Fail-closed: disabled unless ops opts in.

    Reached via the unified Web Agent (site_navigator delegates write actions here).
    "A read cannot spend money" is now enforced by the kill switches below rather than
    by code separation — every path returns the disabled envelope until
    AZTEA_ACTION_WEB_ENABLED=1, so the merged agent reads-only by default.
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
    # dry_run + commit both act on the action url, so enforce the mandate's domain
    # binding here: refuse a url outside the authorized scope (a coerced/mistaken url
    # can be neither navigated for a rehearsal nor acted on).
    if not _url_within_allowed_domains(raw_url, mandate):
        return _err(
            "web_actor.domain_not_allowed",
            "the action url is not within the mandate's allowed_domains. No action taken.",
        )
    if action == "dry_run":
        return _dry_run(
            raw_url, mandate, payload.get("steps"),
            str(payload.get("use_credential") or "").strip().lower(),
        )
    return _commit(mandate, str(payload.get("confirmation_nonce") or "").strip())
