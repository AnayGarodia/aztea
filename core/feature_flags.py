"""
Feature-flag helpers for Aztea.

Flags are read from environment variables at call time (no caching), so a
running server can pick up changes via systemd env-file reload + SIGHUP.

Convention: AZTEA_<FEATURE_NAME>=1|true|yes|on  →  enabled
            AZTEA_<FEATURE_NAME>=0|false|no|off  →  disabled (or missing)

All flags defined here document their default and the version they were
introduced so we know when it's safe to make the default permanent.
"""

from __future__ import annotations

import os


def _read(name: str) -> str:
    return os.environ.get(name, "").strip().lower()


def _truthy(val: str) -> bool:
    return val in {"1", "true", "yes", "on"}


def _falsy(val: str) -> bool:
    return val in {"0", "false", "no", "off"}


def flag(name: str, *, default: bool = False) -> bool:
    """Return the current value of an env-driven boolean flag.

    Falls back to *default* when the variable is unset or empty.
    Explicit "0"/"false" always overrides a True default.
    """
    val = _read(name)
    if not val:
        return default
    if _truthy(val):
        return True
    if _falsy(val):
        return False
    return default


def flag_float(name: str, *, default: float) -> float:
    """Return an env-driven float flag, defaulted on missing/invalid values.

    Used for thresholds (confidence floors, price caps) that should be
    runtime-tunable without redeploying.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Named flags (add new ones here; keep alphabetical)
# ---------------------------------------------------------------------------

# Disable the sentence-transformers embedding model entirely.  When set,
# semantic search degrades to lexical-only; similarity scores are omitted.
# Default: off (embeddings are on).
DISABLE_EMBEDDINGS: bool = flag("AZTEA_DISABLE_EMBEDDINGS", default=False)

# Gate lazy MCP schema loading (5.3 in the roadmap).
# Default: on now that the slim MCP surface is implemented.
LAZY_MCP_SCHEMAS: bool = flag("AZTEA_LAZY_MCP_SCHEMAS", default=True)

# Output truncation: summary mode replaces large blobs and lists with refs.
# Default: on.
OUTPUT_TRUNCATION: bool = flag("AZTEA_OUTPUT_TRUNCATION", default=True)

# Pre-initialised Python interpreter pool for the python_executor agent.
# Off by default — warm pools consume memory even when idle.
PYTHON_WARM_POOL: bool = flag("AZTEA_PYTHON_WARM_POOL", default=False)

# Result-cache v2 (SHA-keyed, per-agent opt-out).
# Default: on.
RESULT_CACHE_V2: bool = flag("AZTEA_RESULT_CACHE_V2", default=True)

# Require an external verifier to approve output before settling payment.
# Off by default: settle immediately and allow clawback via disputes.
REQUIRE_VERIFICATION: bool = flag("AZTEA_REQUIRE_VERIFICATION", default=False)


# ---------------------------------------------------------------------------
# Auto-invoke (aztea_do) — read at call time so thresholds can be tuned
# without restarting the server. Defaults are conservative.
# ---------------------------------------------------------------------------


def auto_invoke_enabled() -> bool:
    """Master switch for the aztea_do auto-invoke meta-tool. Default on."""
    return flag("AZTEA_AUTO_INVOKE_ENABLED", default=True)


def auto_invoke_confidence_floor() -> float:
    """Minimum normalised confidence score (0.0–1.0) to auto-fire a hire.

    Below this floor the endpoint returns a search-style response with
    candidates, no charge. Default 0.30 — Claude Code QA showed obvious
    intents like "Run this Python: print(...)" scoring 0.27–0.30 and
    refusing, which broke the headline UX. Lowering to 0.30 lets clear
    matches through; the trust + price gates still protect spend.
    """
    return flag_float("AZTEA_AUTO_INVOKE_CONFIDENCE", default=0.30)


def auto_invoke_server_cap_usd() -> float:
    """Hard server-side ceiling on auto-invoke per-call price.

    Even if the caller asks for a higher max_cost_usd, this cap wins. Stops
    a misconfigured caller from accidentally hiring expensive agents in a
    loop. Default $0.50.
    """
    return flag_float("AZTEA_AUTO_INVOKE_SERVER_CAP_USD", default=0.50)


def auto_invoke_trust_floor() -> float:
    """Minimum trust score (0–100) to be eligible for auto-invoke.

    Default 50 — the entire current catalog clusters at 52–65 because few
    agents have accumulated enough completed jobs for reputation to mature.
    With the floor at 70 the gate fired 100% of the time and aztea_do never
    auto-invoked. We raise this back toward 70 once the curated catalog has
    at least 10 calls of history per agent.
    """
    return flag_float("AZTEA_AUTO_INVOKE_TRUST_FLOOR", default=50.0)


def auto_invoke_success_floor() -> float:
    """Minimum success rate (0.0–1.0) to be eligible for auto-invoke.

    Default 0.80 — agents with <10 completed jobs sit at the seeded baseline.
    Raised toward 0.90 once history accumulates.
    """
    return flag_float("AZTEA_AUTO_INVOKE_SUCCESS_FLOOR", default=0.80)
