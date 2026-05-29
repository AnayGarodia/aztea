# OWNS: Phase 2 (B2) — flat intent taxonomy used by the classifier and
#       by agent specs to declare which classes they serve.
# NOT OWNS: the classifier itself (intent_classifier.py); scoring
#       (auto_hire.py); per-class success tracking (Phase 3).
# INVARIANTS:
#   - Labels are frozen at module load. Adding a new label requires a
#     migration to the daily rollup table AND a re-tagging operator run.
#   - "other" is the catch-all for genuinely-novel intents. It MUST
#     remain the last entry so dict iteration order keeps "other" as
#     the fallback.
# DECISIONS:
#   - COARSE taxonomy per /autoplan O-3 (user-locked): 7 labels covering
#     the catalog at a meaningful granularity. Tighter classes (e.g.
#     "cve_lookup" vs "secret_scan") were rejected because the picker
#     already has slug + keyword signals for that level of detail; the
#     taxonomy's job is per-class success tracking, not single-agent
#     routing.
from __future__ import annotations

from typing import Final


INTENT_TAXONOMY: Final[dict[str, str]] = {
    "code_execution": (
        "Run a snippet of code in a sandboxed runtime. Python, Node, "
        "Deno, Bun, Go, Rust. Tests, repls, one-off scripts."
    ),
    "code_audit": (
        "Static analysis of code or manifests: lint, type-check, "
        "coverage, dependency CVE audit, secret scanning, SAST. "
        "Reads source; does NOT execute it."
    ),
    "infra_check": (
        "Validate Kubernetes manifests, Terraform plans, Dockerfile, "
        "OpenAPI specs. Schema + best-practices."
    ),
    "live_data": (
        "Look up live external data: CVE / NIST, DNS records, SSL "
        "certificates, package versions, registry metadata, archived "
        "pages."
    ),
    "document_parse": (
        "Extract structured content from PDF, tabular, or form "
        "documents."
    ),
    "web_automation": (
        "Headless browser actions: screenshot, scrape, accessibility "
        "audit (axe-core), Lighthouse, broken-link crawl."
    ),
    "other": (
        "Catch-all for intents that do not fit the categories above. "
        "Treated as the unclassified bucket in the per-class success "
        "rollup."
    ),
}


def is_valid_class(label: str | None) -> bool:
    """Pure: True iff `label` is a known taxonomy entry."""
    return bool(label) and label in INTENT_TAXONOMY


def all_classes() -> tuple[str, ...]:
    """Pure: ordered tuple of all taxonomy labels."""
    return tuple(INTENT_TAXONOMY.keys())


def describe(label: str) -> str | None:
    """Pure: human-readable description for the label, or None."""
    return INTENT_TAXONOMY.get(label)


__all__ = ["INTENT_TAXONOMY", "all_classes", "describe", "is_valid_class"]
