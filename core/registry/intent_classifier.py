# OWNS: Phase 2 (B2) — classify a natural-language intent into one of
#       the taxonomy labels. Cache per intent hash. Never blocks the
#       scoring hot path: classify in background on first sight,
#       second identical decision picks up the cached label.
# NOT OWNS: the taxonomy itself (intent_taxonomy.py); scoring
#       (auto_hire.py); per-class success tracking (Phase 3).
# INVARIANTS:
#   - classify() never raises. Returns None on any LLM failure;
#     callers treat None as "unclassified."
#   - First call on a new intent hash returns None synchronously and
#     fires a background thread to populate the cache. Second identical
#     call returns the cached label without re-running the LLM.
# DECISIONS:
#   - Rule-based fast path first (cheap, deterministic). LLM only when
#     the rules can't pick a confident class. Keeps cost negligible for
#     the high-volume long-tail of routine intents.
#   - Cache is per-process (lru_cache). Process bounce = recomputation;
#     acceptable because classify() is bounded-cost.
# KNOWN DEBT:
#   - No persistence of classifier output across process bounces.
#     A small SQLite-backed cache would amortize cost in busy envs.
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from functools import lru_cache

from core.registry.intent_taxonomy import INTENT_TAXONOMY, is_valid_class

logger = logging.getLogger(__name__)

_CLASSIFIER_LLM_ENABLED = (
    os.environ.get("AZTEA_INTENT_CLASSIFIER_LLM", "1").lower() != "0"
)
_LLM_MAX_TOKENS = 16
_LLM_TEMPERATURE = 0.0

# Background classification dispatch (so first-sight intents never
# block the hot path). Keyed on intent_hash so two simultaneous calls
# on the same intent dedupe to one LLM call.
#
# /review M4 (2026-05-28): semaphore caps simultaneous background
# classifier threads. Without it, a burst of unique intents could
# spawn unbounded daemon threads — process resource exhaustion.
_inflight_hashes: set[str] = set()
_inflight_lock = threading.Lock()
_BACKGROUND_THREAD_CAP = int(
    os.environ.get("AZTEA_INTENT_CLASSIFIER_THREAD_CAP", "8")
)
_thread_semaphore = threading.Semaphore(_BACKGROUND_THREAD_CAP)


# Rule-based fast path. Tight keyword sets per class. Returns the
# class label when confident, None when ambiguous (LLM fallback).
_RULE_MAP: tuple[tuple[str, frozenset[str]], ...] = (
    ("live_data", frozenset({
        "cve", "cves", "nvd", "dns", "ssl", "tls", "cert",
        "certificate", "whois", "registry", "lookup",
    })),
    ("code_execution", frozenset({
        "run", "execute", "evaluate", "repl", "interpret",
        "python", "node", "deno", "bun", "javascript", "typescript",
        "go", "rust", "script", "snippet",
    })),
    ("code_audit", frozenset({
        # Note: "cve" intentionally NOT here — bare CVE lookup is
        # live_data, manifest auditing is code_audit. Disambiguate
        # via the manifest filenames + audit verbs.
        "audit", "scan", "lint", "vulnerability", "vulnerabilities",
        "secret", "credentials", "sast", "ruff", "mypy",
        "tsc", "coverage", "package.json", "requirements.txt",
    })),
    ("infra_check", frozenset({
        "kubernetes", "k8s", "kubectl", "manifest", "terraform",
        "tf", "hcl", "dockerfile", "openapi", "swagger",
    })),
    ("document_parse", frozenset({
        "pdf", "tabular", "extract", "form", "document", "parse",
    })),
    ("web_automation", frozenset({
        "screenshot", "scrape", "browser", "playwright",
        "lighthouse", "accessibility", "axe", "crawl",
    })),
)


def _hash_intent(intent_text: str) -> str:
    return hashlib.sha256(intent_text.encode("utf-8", errors="replace")).hexdigest()


def _rule_classify(intent_lower: str) -> str | None:
    """Pure: return a label when the intent has unambiguous keyword signal."""
    tokens = set(re.findall(r"[a-z0-9_.]+", intent_lower))
    hits: dict[str, int] = {}
    for label, kws in _RULE_MAP:
        n = sum(1 for kw in kws if kw in tokens or kw in intent_lower)
        if n:
            hits[label] = n
    if not hits:
        return None
    # Require dominance: top class must beat runner-up by 2+ hits.
    sorted_hits = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(sorted_hits) == 1 or sorted_hits[0][1] >= sorted_hits[1][1] + 2:
        return sorted_hits[0][0]
    return None


def _llm_classify(
    intent_text: str, *, caller_owner_id: str | None = None,
) -> str | None:
    """Side-effect: ask an LLM to pick one taxonomy label. Returns None on failure.

    /cso H1: ``caller_owner_id`` keyed into the per-caller budget so
    one caller can't drain the global classifier bucket.
    """
    if not _CLASSIFIER_LLM_ENABLED:
        return None
    from core.registry import _llm_budget
    if not _llm_budget.try_consume(
        "classifier", caller_owner_id=caller_owner_id,
    ):
        logger.debug("intent_classifier: budget exhausted, returning None")
        return None
    try:
        from core.llm import CompletionRequest, Message, run_with_fallback
    except Exception:  # noqa: BLE001
        return None
    labels = ", ".join(INTENT_TAXONOMY.keys())
    descriptions = "\n".join(
        f"- {label}: {desc}" for label, desc in INTENT_TAXONOMY.items()
    )
    system = (
        "You classify a user intent into exactly one category. "
        "Respond with ONLY the category name (one of: "
        f"{labels}). No explanation, no code fences."
    )
    user = f"Categories:\n{descriptions}\n\nIntent: {intent_text.strip()[:1000]}"
    try:
        response = run_with_fallback(
            CompletionRequest(
                model="",  # fallback chain picks the model
                messages=[
                    Message(role="system", content=system),
                    Message(role="user", content=user),
                ],
                temperature=_LLM_TEMPERATURE,
                max_tokens=_LLM_MAX_TOKENS,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("intent_classifier: LLM call failed: %s", exc)
        return None
    text = (response.text or "").strip().lower()
    # Tolerant matching: strip whitespace, punctuation; check exact match.
    candidate = re.sub(r"[^a-z_]", "", text)
    return candidate if is_valid_class(candidate) else None


@lru_cache(maxsize=2048)
def _classify_cached(intent_lower: str) -> str | None:
    """Pure-ish (LLM call is impure, but result is deterministic at fixed temp).

    Cache keyed on lowercased intent. lru_cache deduplicates calls on
    the same intent string across the process lifetime. Never raises:
    LLM failures return None so the caller treats the intent as
    unclassified rather than crashing the scoring path.

    Note: the cache deliberately ignores caller identity — the
    classification of "lookup CVE-2021-44228" is the same across all
    callers, and that's the point of the cache. The per-caller budget
    gate runs INSIDE _llm_classify on cache misses; cache hits don't
    consume budget at all.
    """
    rule = _rule_classify(intent_lower)
    if rule is not None:
        return rule
    try:
        return _llm_classify(intent_lower)
    except Exception as exc:  # noqa: BLE001 — never crash scoring
        logger.debug("intent_classifier: classify_cached LLM failed: %s", exc)
        return None


def classify(intent_text: str, *, allow_background: bool = True) -> str | None:
    """Synchronous classifier — returns a taxonomy label or None.

    When ``allow_background=True`` and the LLM path isn't already cached,
    this returns None immediately and fires a background thread to
    populate the cache so the NEXT identical intent picks up the label
    without paying the LLM round-trip in the hot path. Use
    ``allow_background=False`` from tests or non-hot-path callers
    (e.g. retention sweep) to block until the LLM responds.
    """
    text = (intent_text or "").strip()
    if not text:
        return None
    text_lower = text.lower()

    # Rule-path is cheap — always run it synchronously.
    rule = _rule_classify(text_lower)
    if rule is not None:
        return rule

    if not allow_background:
        return _classify_cached(text_lower)

    intent_hash = _hash_intent(text_lower)
    with _inflight_lock:
        already_inflight = intent_hash in _inflight_hashes
        if not already_inflight:
            _inflight_hashes.add(intent_hash)

    if already_inflight:
        # Don't spawn a duplicate worker; return None and let the
        # other thread populate the cache.
        return None

    # /review M4: respect the global thread cap. If we can't acquire a
    # slot now, return None and let the user retry — better than
    # blocking or spawning unbounded threads.
    if not _thread_semaphore.acquire(blocking=False):
        with _inflight_lock:
            _inflight_hashes.discard(intent_hash)
        logger.debug(
            "intent_classifier: background thread cap reached "
            "(_BACKGROUND_THREAD_CAP=%d)", _BACKGROUND_THREAD_CAP,
        )
        return None

    def _populate() -> None:
        try:
            _classify_cached(text_lower)
        except Exception:  # noqa: BLE001 — never crash the worker
            logger.exception("intent_classifier: background populate failed")
        finally:
            _thread_semaphore.release()
            with _inflight_lock:
                _inflight_hashes.discard(intent_hash)

    threading.Thread(target=_populate, daemon=True).start()
    return None


__all__ = ["classify"]
