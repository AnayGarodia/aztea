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


# ---------------------------------------------------------------------------
# Search ranking thresholds (read at call time, see core/registry/agents_ops.py)
# Introduced post-2026-05-08 eval to make the empty-result floor and dropoff
# band tunable without a redeploy.  Defaults preserve current behavior.
# ---------------------------------------------------------------------------


def search_relevance_floor() -> float:
    """Top blended score below which the search returns an empty list.

    Rationale: returning weak distractors creates false confidence in low-
    relevance results. Empty signals "use a different query".
    """
    return flag_float("AZTEA_SEARCH_RELEVANCE_FLOOR", default=0.18)


def search_keep_floor() -> float:
    """Per-result blended score below which an item is dropped post-ranking,
    unless within the dropoff band of the top hit."""
    return flag_float("AZTEA_SEARCH_KEEP_FLOOR", default=0.20)


def search_dropoff_band() -> float:
    """Score band relative to top hit; results within it survive even when
    they fall under the keep floor."""
    return flag_float("AZTEA_SEARCH_DROPOFF_BAND", default=0.20)


def search_content_floor() -> float:
    """Minimum semantic similarity for a candidate to count as a real
    content match when its lexical score is below the lexical floor.

    Sized against sentence-transformers MiniLM (the default embedding
    model): unrelated short queries cosine ~0.10–0.20 against arbitrary
    agent descriptions; vaguely related ~0.30–0.45; truly relevant
    queries score 0.50+. The 0.45 default sits just above the noise
    band — any candidate that clears it has real semantic relation to
    the query. Lower values let off-catalog queries through (the eval's
    "tell me a joke" returning code agents); higher values block
    legitimate matches when the agent description doesn't share many
    surface words with the query.
    """
    return flag_float("AZTEA_SEARCH_CONTENT_FLOOR", default=0.45)


def search_lexical_content_floor() -> float:
    """Minimum lexical match score for a candidate to count as a real
    content match when its semantic similarity is below the content floor.

    Rationale: the lexical scorer awards small weights for any token
    overlap. A query like "tell me a joke" against an agent description
    containing "me" (e.g. "use me for X") produces lexical_score ~0.02 —
    technically nonzero, but functionally noise. Requiring >= 0.10 here
    means at least one substantive match (a content word, not a
    one-letter coincidence). Tunable so production can adjust if the
    underlying lexical scorer is retuned.
    """
    return flag_float("AZTEA_SEARCH_LEXICAL_FLOOR", default=0.10)


def search_llm_rerank_enabled() -> bool:
    """Master switch for the optional LLM re-rank stage in agent search.

    Off by default: the deterministic lexical+embedding+trust ranker is
    fast, cheap, and accurate on the current ~10-agent catalog. Flip on
    once the catalog grows past ~30 agents and ambiguous intent queries
    start surfacing wrong agents at the top. The stage runs only when the
    top candidates are clustered in the fuzzy zone (top score in
    [content_floor, content_floor+0.15]) — clear winners and clear
    off-catalog queries skip it. See core/registry/agents_ops.py.
    """
    return flag("AZTEA_SEARCH_LLM_RERANK", default=False)

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

    Default 30 — the curated catalog still has sparse ratings, so trust is a
    weak blocking signal for clear low-cost matches. Confidence, stability,
    success history, schema validation, and price caps remain active gates.
    """
    return flag_float("AZTEA_AUTO_INVOKE_TRUST_FLOOR", default=30.0)


def auto_invoke_success_floor() -> float:
    """Minimum success rate (0.0–1.0) to be eligible for auto-invoke.

    Default 0.50 — most curated builtins sit between 0.40 and 0.80 because
    the success counter mixes pre-fix schema rejections in with real failures.
    A 0.80 floor effectively disables auto-invoke for most of the catalog,
    which silently breaks `aztea_do`. Raise back toward 0.80 after the
    rolling-window stats have stabilized post-deprecation cleanup
    (2026-05-07).
    """
    return flag_float("AZTEA_AUTO_INVOKE_SUCCESS_FLOOR", default=0.80)
