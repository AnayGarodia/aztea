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


def flag_int(name: str, *, default: int) -> int:
    """Return an env-driven integer flag, defaulted on missing/invalid values.

    Used for hard caps (rate limits, max iterations) tunable without redeploy.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
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

# Per-key sliding-window rate limits, evaluated as transport-layer middleware.
# Read at module import — no env hot-reload — because middleware is registered
# once at startup and the request hot path must not pay an env lookup. Tune via
# AZTEA_RATE_LIMIT_* and restart to roll out new limits.
RATE_LIMIT_DEFAULT_RPM: int = flag_int("AZTEA_RATE_LIMIT_DEFAULT_RPM", default=120)
RATE_LIMIT_BURST_RPS: int = flag_int("AZTEA_RATE_LIMIT_BURST_RPS", default=10)
RATE_LIMIT_WORKER_RPM: int = flag_int("AZTEA_RATE_LIMIT_WORKER_RPM", default=600)
RATE_LIMIT_ANON_RPM: int = flag_int("AZTEA_RATE_LIMIT_ANON_RPM", default=60)
# Memory bound on the per-key sliding-window store. Above this many distinct
# keys the oldest-touched entries are LRU-evicted so an attacker cannot OOM
# the worker by cycling through unique synthetic keys.
RATE_LIMIT_MAX_TRACKED_KEYS: int = flag_int(
    "AZTEA_RATE_LIMIT_MAX_TRACKED_KEYS", default=50_000,
)

# Result-cache v2 (SHA-keyed, per-agent opt-out).
# Default: on.
RESULT_CACHE_V2: bool = flag("AZTEA_RESULT_CACHE_V2", default=True)


# ---------------------------------------------------------------------------
# Search ranking thresholds (read at call time, see core/registry/agents_ops.py).
# Tunable without a redeploy via env vars.
# ---------------------------------------------------------------------------


def search_relevance_floor() -> float:
    """Top blended score below which the search returns an empty list.

    Rationale: returning weak distractors creates false confidence in
    low-relevance results. Empty signals "use a different query".

    Default calibrated against measured off-catalog query distribution:
    blended_score for queries like "tell me a joke" or "cook me dinner"
    lands at 0.23–0.26 (carried by trust + price when content overlap is
    in the noise band). Legitimate
    queries cluster at 0.33+. The 0.30 floor sits cleanly between the
    two distributions.
    """
    return flag_float("AZTEA_SEARCH_RELEVANCE_FLOOR", default=0.30)


def search_keep_floor() -> float:
    """Per-result blended score below which an item is dropped post-ranking,
    unless within the dropoff band of the top hit."""
    return flag_float("AZTEA_SEARCH_KEEP_FLOOR", default=0.20)


def search_dropoff_band() -> float:
    """Score band relative to top hit; results within it survive even when
    they fall under the keep floor."""
    return flag_float("AZTEA_SEARCH_DROPOFF_BAND", default=0.20)


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


# ---------------------------------------------------------------------------
# Probation auto-graduation thresholds (read at call time so an operator can
# loosen / tighten without restarting the server). Probation listings stay
# rank-penalised and price-capped (core/registry/auto_hire.py) until they
# clear ALL of these gates, at which point the sweeper transitions them to
# 'approved'. CLAUDE.md §"Adding a third-party agent" advertises this.
# ---------------------------------------------------------------------------


def probation_min_successes() -> int:
    """Minimum `successful_calls` to be eligible for graduation.

    Default 5: enough signal to distinguish a working agent from a one-shot
    fluke without forcing publishers to grind through dozens of paid calls
    before their listing escapes the rank penalty.
    """
    raw = os.environ.get("AZTEA_PROBATION_MIN_SUCCESSES", "").strip()
    try:
        return max(1, int(raw)) if raw else 5
    except (TypeError, ValueError):
        return 5


def probation_min_success_rate() -> float:
    """Minimum success rate (successful_calls / total_calls)."""
    return flag_float("AZTEA_PROBATION_MIN_SUCCESS_RATE", default=0.80)


def probation_min_quality() -> float:
    """Minimum average quality rating (1.0–5.0) on rated jobs."""
    return flag_float("AZTEA_PROBATION_MIN_QUALITY", default=3.5)


def probation_min_age_hours() -> float:
    """Minimum age (hours since `created_at`) before graduation is considered.

    Guards against a publisher gaming the gate with a burst of self-calls
    in the first hour. Default 24h.
    """
    return flag_float("AZTEA_PROBATION_MIN_AGE_HOURS", default=24.0)


def probation_sweep_interval_seconds() -> float:
    """How often the job sweeper runs the graduation pass.

    The job sweeper itself ticks every ~2s; a per-tick graduation query
    would be wasteful. Default 5 minutes.
    """
    return flag_float("AZTEA_PROBATION_SWEEP_INTERVAL_S", default=300.0)


def auto_invoke_success_floor() -> float:
    """Minimum success rate (0.0–1.0) to be eligible for auto-invoke.

    Default 0.80. The success counter mixes pre-fix schema rejections in with
    real failures, so most curated builtins sit between 0.40 and 0.80; a 0.80
    floor effectively disables auto-invoke for most of the catalog. Lower the
    env var temporarily until rolling-window stats stabilize.
    """
    return flag_float("AZTEA_AUTO_INVOKE_SUCCESS_FLOOR", default=0.80)


def auto_invoke_embeddings_enabled() -> bool:
    """Enable the semantic-similarity term in auto-hire routing.

    Default ON. Lexical signals (slug, name, description tokens, curated
    keywords) continue to score regardless; the embedding term is additive
    and capped, so a misfiring backend cannot starve the lexical winner.
    Flip OFF (`AZTEA_AUTO_INVOKE_EMBEDDINGS=0`) if a specific catalog finds
    semantic scores dominating clearly-keyword-matched intents.
    """
    return flag("AZTEA_AUTO_INVOKE_EMBEDDINGS", default=True)


# ---------------------------------------------------------------------------
# OSS / hosted boundary
#
# Aztea ships as Apache-2.0 OSS that runs fully self-contained by default.
# The hosted aztea.ai deployment turns on extra services (judges using our
# LLM credits, public registry syndication, federated reputation, real
# Stripe Connect money movement). The flags below are the single switch
# governing whether those services are reachable from this instance.
#
# INVARIANT: when AZTEA_HOSTED_API_URL is unset, no module may make a
# network call to aztea.ai. All hosted-service calls go through
# core/hosted_client.py, which short-circuits to None / local fallback when
# is_enabled() returns False.
# ---------------------------------------------------------------------------


def hosted_mode_enabled() -> bool:
    """True iff this instance is configured to call out to aztea.ai's hosted API.

    Read at call time so a deploy can flip between local and hosted without
    a restart by setting AZTEA_HOSTED_API_URL in the env.
    """
    return bool(os.environ.get("AZTEA_HOSTED_API_URL", "").strip())


def hosted_api_url() -> str:
    """Base URL for hosted aztea.ai services. Empty string when disabled."""
    return os.environ.get("AZTEA_HOSTED_API_URL", "").strip().rstrip("/")


def hosted_api_key() -> str:
    """Bearer token for the hosted API. Empty string when not configured."""
    return os.environ.get("AZTEA_HOSTED_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Vibe-an-agent (self-serve agent generation from natural-language description)
# Read at call time so a deploy can flip without restart. Defaults are OFF
# in OSS — generation is opt-in and rate-limited per owner.
# ---------------------------------------------------------------------------


def agent_generation_enabled() -> bool:
    """Master switch for POST /agents/generate. Default OFF in OSS."""
    return flag("AZTEA_AGENT_GENERATION_ENABLED", default=False)


def agent_generation_clone_threshold() -> float:
    """Cosine similarity (0.0–1.0) above which a generated agent is rejected
    as a near-clone of an existing approved/probation listing. Default 0.92.
    """
    return flag_float("AZTEA_AGENT_GENERATION_CLONE_THRESHOLD", default=0.92)


def agent_generation_max_per_day() -> int:
    """Per-owner cap on generation attempts in a UTC day. Default 20.
    Platform-wide cap is 10x this, enforced at handler level.
    """
    return flag_int("AZTEA_AGENT_GENERATION_MAX_PER_DAY", default=20)


def stripe_enabled() -> bool:
    """True iff Stripe topup/withdraw/Connect routes are wired up.

    Computed from the presence of STRIPE_SECRET_KEY rather than a separate
    flag, so the env stays minimal: configure Stripe → Stripe routes work;
    omit Stripe → the routes return 501 with a pointer to hosted aztea.ai.
    """
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


# ---------------------------------------------------------------------------
# Ledger reconciliation auto-repair
# ---------------------------------------------------------------------------

# Maximum |drift| in cents that `POST /ops/payments/reconcile?auto_repair=1`
# will auto-fix per wallet. Drift larger than this is almost always a real
# bug rather than a stale cache, so we surface it for human review instead
# of silently rewriting state. $100 = 10000 cents.
AUTO_REPAIR_THRESHOLD_CENTS = 10000


def auto_repair_threshold_cents() -> int:
    """Runtime-tunable variant of AUTO_REPAIR_THRESHOLD_CENTS.

    Reading at call time lets ops bump the ceiling during a controlled
    backfill (`AZTEA_AUTO_REPAIR_THRESHOLD_CENTS=50000`) without restarting
    uvicorn. Falls back to the module-level constant when the env is unset
    or contains a non-integer value.
    """
    return flag_int(
        "AZTEA_AUTO_REPAIR_THRESHOLD_CENTS",
        default=AUTO_REPAIR_THRESHOLD_CENTS,
    )
